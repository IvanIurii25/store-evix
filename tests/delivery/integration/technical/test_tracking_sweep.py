"""The Nova Post tracking sweep (phase P5).

The carrier never calls us back, so a shipment's status only moves when this
task asks. What matters: it asks only about parcels still in flight, it stores
a machine-readable code next to the carrier's wording, it does not churn rows
that did not change, and a carrier outage costs this run rather than the data.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.order import Order, OrderDeliveryNovaPost
from app.tasks.novapost import _sync

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def carrier_on(monkeypatch):
    """Run the sweep against the stub carrier."""
    monkeypatch.setattr(settings, "novapost_mode", "stub")


async def _order_with_waybill(
    session: AsyncSession,
    number: str,
    *,
    awb: str | None,
    status_text: str = "",
    status_code: str = "",
) -> OrderDeliveryNovaPost:
    """Persist a carrier order, optionally already shipped."""
    order = Order(
        number=number,
        user_id=None,
        email="buyer@example.com",
        phone="+37360000000",
        status="new",
        payment_status="pending",
        subtotal=Decimal("100"),
        discount_total=Decimal("0"),
        delivery_cost=Decimal("60"),
        total=Decimal("160"),
        delivery_type="branch",
        delivery_service="novapost",
        delivery_address_id=None,
        payment_method="cod",
    )
    session.add(order)
    await session.flush()
    row = OrderDeliveryNovaPost(
        order_id=order.id,
        settlement_id="s-1",
        settlement_name="Chișinău",
        division_id="d-1",
        division_number="1",
        division_address="str. Ștefan cel Mare 12",
        awb_number=awb,
        awb_id=awb,
        status_code=status_code,
        status_text=status_text,
    )
    session.add(row)
    await session.flush()
    return row


async def test_sweep_stores_code_and_text(db_session: AsyncSession) -> None:
    """A fresh status is stored as a filterable code plus the carrier's wording."""
    row = await _order_with_waybill(db_session, "TRK-001", awb="STUB00000001")

    changed = await _sync(db_session)

    await db_session.refresh(row)
    assert changed == 1
    assert row.status_code == "10"
    assert row.status_text == "Accepted"
    assert row.status_updated_at is not None


async def test_sweep_skips_terminal_shipments(db_session: AsyncSession) -> None:
    """A delivered parcel is never asked about again.

    Otherwise the sweep would grow with order history forever, polling a third
    party about parcels that cannot change.
    """
    row = await _order_with_waybill(
        db_session, "TRK-002", awb="STUB00000002", status_text="Delivered"
    )

    changed = await _sync(db_session)

    await db_session.refresh(row)
    assert changed == 0
    assert row.status_text == "Delivered"


async def test_sweep_ignores_orders_without_a_waybill(
    db_session: AsyncSession,
) -> None:
    """An order that was never shipped has nothing to track."""
    row = await _order_with_waybill(db_session, "TRK-003", awb=None)

    changed = await _sync(db_session)

    await db_session.refresh(row)
    assert changed == 0
    assert row.status_text == ""


async def test_unchanged_status_is_not_rewritten(db_session: AsyncSession) -> None:
    """A repeat answer touches nothing — no pointless writes, no false timestamps."""
    row = await _order_with_waybill(
        db_session,
        "TRK-004",
        awb="STUB00000004",
        status_code="10",
        status_text="Accepted",
    )

    changed = await _sync(db_session)

    await db_session.refresh(row)
    assert changed == 0
    assert row.status_updated_at is None


async def test_carrier_outage_leaves_rows_open(
    db_session: AsyncSession, monkeypatch
) -> None:
    """A failed sweep changes nothing; the same rows are retried next time."""
    from app.services.delivery import novapost_stub
    from app.services.delivery.novapost_client import NovaPostError

    async def boom(self, numbers):
        raise NovaPostError("carrier down")

    monkeypatch.setattr(novapost_stub.NovaPostStub, "tracking", boom)
    row = await _order_with_waybill(db_session, "TRK-005", awb="STUB00000005")

    changed = await _sync(db_session)

    await db_session.refresh(row)
    assert changed == 0
    assert row.status_text == ""


async def test_sweep_is_a_noop_when_the_carrier_is_off(
    db_session: AsyncSession, monkeypatch
) -> None:
    """With the integration disabled the task does nothing at all."""
    monkeypatch.setattr(settings, "novapost_mode", "")
    await _order_with_waybill(db_session, "TRK-006", awb="STUB00000006")

    assert await _sync(db_session) == 0


async def test_sweep_updates_every_open_shipment(db_session: AsyncSession) -> None:
    """All open parcels are refreshed, not just the first one."""
    await _order_with_waybill(db_session, "TRK-007", awb="STUB00000007")
    await _order_with_waybill(db_session, "TRK-008", awb="STUB00000008")

    changed = await _sync(db_session)

    rows = (
        (
            await db_session.execute(
                select(OrderDeliveryNovaPost).where(
                    OrderDeliveryNovaPost.awb_number.in_(
                        ["STUB00000007", "STUB00000008"]
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    assert changed == 2
    assert all(row.status_text == "Accepted" for row in rows)
