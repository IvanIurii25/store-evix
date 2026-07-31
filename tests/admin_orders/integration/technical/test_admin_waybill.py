"""Back-office waybill operations (Nova Post phase P4).

A waybill is a purchase: creating one costs money and puts a parcel in motion.
So the properties pinned here are about not doing it twice, not lying about
whether it happened, and never leaving a cancelled order with a live shipment.

Runs against the stub carrier — no credentials, no network.
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.order import Order, OrderDeliveryNovaPost

pytestmark = pytest.mark.asyncio

_ADMIN = "/api/v1/admin/orders"


@pytest.fixture(autouse=True)
def carrier_on(monkeypatch):
    """Serve these tests through the stub carrier."""
    monkeypatch.setattr(settings, "novapost_mode", "stub")
    monkeypatch.setattr(settings, "novapost_sender_division_id", "d-9")
    monkeypatch.setattr(settings, "novapost_sender_name", "evix")
    monkeypatch.setattr(settings, "novapost_sender_phone", "+37360000001")


async def _carrier_order(
    db_session: AsyncSession,
    make_order,
    number: str,
    *,
    awb: str | None = None,
    status: str = "new",
) -> Order:
    """Persist an order that ships with the carrier, optionally with a waybill."""
    order = await make_order(number, status=status)
    order.delivery_service = "novapost"
    order.delivery_type = "branch"
    order.delivery_name = "Ion Client"
    db_session.add(
        OrderDeliveryNovaPost(
            order_id=order.id,
            settlement_id="s-1",
            settlement_name="Chișinău",
            division_id="d-1",
            division_number="1",
            division_address="str. Ștefan cel Mare 12",
            calculated_cost=Decimal("60.00"),
            parcel_weight_g=1500,
            awb_number=awb,
            awb_id=awb,
        )
    )
    await db_session.flush()
    return order


async def _row(db_session: AsyncSession, order_id: int) -> OrderDeliveryNovaPost:
    """Reload the carrier row."""
    return (
        await db_session.execute(
            select(OrderDeliveryNovaPost).where(
                OrderDeliveryNovaPost.order_id == order_id
            )
        )
    ).scalar_one()


async def test_creates_a_waybill(
    client: AsyncClient, db_session: AsyncSession, make_order
) -> None:
    """Creating a waybill stores its number and returns it on the order."""
    order = await _carrier_order(db_session, make_order, "AWB-001")

    resp = await client.post(f"{_ADMIN}/{order.number}/np/shipment")

    assert resp.status_code == 200, resp.text
    assert resp.json()["novapost"]["awb_number"].startswith("STUB")
    row = await _row(db_session, order.id)
    assert row.awb_number and row.awb_id
    # The raw carrier response is kept for support questions later.
    assert row.awb_data


async def test_second_attempt_is_refused(
    client: AsyncClient, db_session: AsyncSession, make_order
) -> None:
    """A double click must not buy a second shipment for the same order."""
    order = await _carrier_order(db_session, make_order, "AWB-002")
    first = await client.post(f"{_ADMIN}/{order.number}/np/shipment")
    assert first.status_code == 200, first.text
    number = first.json()["novapost"]["awb_number"]

    second = await client.post(f"{_ADMIN}/{order.number}/np/shipment")

    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "awb_already_exists"
    assert (await _row(db_session, order.id)).awb_number == number


async def test_own_logistics_order_has_nothing_to_ship(
    client: AsyncClient, make_order
) -> None:
    """A pickup order is not a carrier order — 404, not a stray waybill."""
    order = await make_order("AWB-003")

    resp = await client.post(f"{_ADMIN}/{order.number}/np/shipment")

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "awb_not_found"


async def test_carrier_failure_writes_nothing(
    client: AsyncClient, db_session: AsyncSession, make_order, monkeypatch
) -> None:
    """A refused creation leaves no half-state: no number, no data.

    Telling an operator a waybill exists when it does not is worse than making
    them retry.
    """
    from app.services.delivery import novapost_stub
    from app.services.delivery.novapost_client import NovaPostError

    async def boom(self, payload):
        raise NovaPostError("carrier down")

    monkeypatch.setattr(novapost_stub.NovaPostStub, "create_shipment", boom)
    order = await _carrier_order(db_session, make_order, "AWB-004")

    resp = await client.post(f"{_ADMIN}/{order.number}/np/shipment")

    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "carrier_unavailable"
    row = await _row(db_session, order.id)
    assert row.awb_number is None
    assert row.awb_data is None


async def test_cancels_a_waybill(
    client: AsyncClient, db_session: AsyncSession, make_order
) -> None:
    """Cancelling clears the waybill and its tracking status."""
    order = await _carrier_order(db_session, make_order, "AWB-005", awb="STUB1")

    resp = await client.delete(f"{_ADMIN}/{order.number}/np/shipment")

    assert resp.status_code == 200, resp.text
    assert resp.json()["novapost"]["awb_number"] is None
    row = await _row(db_session, order.id)
    assert row.awb_number is None
    assert row.status_text == ""


async def test_cancelling_without_a_waybill_is_404(
    client: AsyncClient, db_session: AsyncSession, make_order
) -> None:
    """There is nothing to cancel before one is created."""
    order = await _carrier_order(db_session, make_order, "AWB-006")

    resp = await client.delete(f"{_ADMIN}/{order.number}/np/shipment")

    assert resp.status_code == 404, resp.text


async def test_failed_cancellation_keeps_our_copy(
    client: AsyncClient, db_session: AsyncSession, make_order, monkeypatch
) -> None:
    """If the carrier will not cancel, we keep the number.

    Clearing it while the parcel still travels would hide a live shipment from
    the people who have to deal with it.
    """
    from app.services.delivery import novapost_stub
    from app.services.delivery.novapost_client import NovaPostError

    async def boom(self, shipment_id):
        raise NovaPostError("carrier down")

    monkeypatch.setattr(novapost_stub.NovaPostStub, "delete_shipment", boom)
    order = await _carrier_order(db_session, make_order, "AWB-007", awb="STUB9")

    resp = await client.delete(f"{_ADMIN}/{order.number}/np/shipment")

    assert resp.status_code == 502, resp.text
    assert (await _row(db_session, order.id)).awb_number == "STUB9"


async def test_cancelling_an_order_cancels_its_waybill(
    client: AsyncClient, db_session: AsyncSession, make_order
) -> None:
    """A cancelled order must not leave a parcel travelling to the customer."""
    order = await _carrier_order(db_session, make_order, "AWB-008", awb="STUB7")

    resp = await client.post(
        f"{_ADMIN}/{order.number}/transition", json={"to_status": "canceled"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "canceled"
    assert (await _row(db_session, order.id)).awb_number is None


async def test_order_cancellation_fails_closed(
    client: AsyncClient, db_session: AsyncSession, make_order, monkeypatch
) -> None:
    """If the waybill cannot be cancelled, the order is not cancelled either.

    Our records claiming "cancelled" while the carrier still delivers is the
    expensive failure; making an operator retry is the cheap one.
    """
    from app.services.delivery import novapost_stub
    from app.services.delivery.novapost_client import NovaPostError

    async def boom(self, shipment_id):
        raise NovaPostError("carrier down")

    monkeypatch.setattr(novapost_stub.NovaPostStub, "delete_shipment", boom)
    order = await _carrier_order(db_session, make_order, "AWB-009", awb="STUB8")

    resp = await client.post(
        f"{_ADMIN}/{order.number}/transition", json={"to_status": "canceled"}
    )

    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "waybill_cancel_failed"
    refreshed = (
        await db_session.execute(select(Order).where(Order.id == order.id))
    ).scalar_one()
    assert refreshed.status == "new"
    assert (await _row(db_session, order.id)).awb_number == "STUB8"


async def test_cancelling_an_own_order_is_unaffected(
    client: AsyncClient, make_order
) -> None:
    """Orders without a carrier keep cancelling exactly as before."""
    order = await make_order("AWB-010")

    resp = await client.post(
        f"{_ADMIN}/{order.number}/transition", json={"to_status": "canceled"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "canceled"


async def test_waybill_declares_the_quoted_weight(
    client: AsyncClient, db_session: AsyncSession, make_order, monkeypatch
) -> None:
    """The parcel uses the weight snapshotted at checkout, not today's catalogue.

    Products get edited and lines can lose their product entirely; recomputing
    later would silently declare a different parcel than the customer paid for.
    """
    from app.services.delivery import novapost_stub

    seen: list[dict] = []
    original = novapost_stub.NovaPostStub.create_shipment

    async def capture(self, payload):
        seen.append(payload)
        return await original(self, payload)

    monkeypatch.setattr(novapost_stub.NovaPostStub, "create_shipment", capture)
    order = await _carrier_order(db_session, make_order, "AWB-011")

    await client.post(f"{_ADMIN}/{order.number}/np/shipment")

    parcel = seen[0]["parcels"][0]
    assert parcel["actualWeight"] == 1500
    assert seen[0]["recipient"]["name"] == "Ion Client"
    assert seen[0]["recipient"]["divisionId"] == "d-1"
    assert seen[0]["sender"]["divisionId"] == "d-9"
    assert seen[0]["clientOrder"] == order.number


async def test_waybill_endpoints_require_staff(
    guest_client: AsyncClient, db_session: AsyncSession, make_order
) -> None:
    """Buying shipments is staff-only."""
    order = await _carrier_order(db_session, make_order, "AWB-012")

    created = await guest_client.post(f"{_ADMIN}/{order.number}/np/shipment")
    cancelled = await guest_client.delete(f"{_ADMIN}/{order.number}/np/shipment")

    assert created.status_code in (401, 403), created.text
    assert cancelled.status_code in (401, 403), cancelled.text


async def test_creating_a_waybill_emails_the_tracking_number(
    client: AsyncClient, db_session: AsyncSession, make_order, monkeypatch
) -> None:
    """The customer is told how to track the parcel, with its destination."""
    from app.tasks import waybill_email

    sent: list[dict] = []
    monkeypatch.setattr(
        waybill_email.send_waybill_email,
        "delay",
        lambda **kwargs: sent.append(kwargs),
    )
    order = await _carrier_order(db_session, make_order, "AWB-013")

    await client.post(f"{_ADMIN}/{order.number}/np/shipment")

    assert len(sent) == 1
    assert sent[0]["to"] == order.email
    assert sent[0]["awb_number"].startswith("STUB")
    assert "str. Ștefan cel Mare 12" in sent[0]["destination"]


async def test_a_broker_outage_does_not_undo_the_waybill(
    client: AsyncClient, db_session: AsyncSession, make_order, monkeypatch
) -> None:
    """The shipment exists at the carrier; a mail hiccup must not fail the call."""
    from app.tasks import waybill_email

    def boom(**_kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(waybill_email.send_waybill_email, "delay", boom)
    order = await _carrier_order(db_session, make_order, "AWB-014")

    resp = await client.post(f"{_ADMIN}/{order.number}/np/shipment")

    assert resp.status_code == 200, resp.text
    assert (await _row(db_session, order.id)).awb_number
