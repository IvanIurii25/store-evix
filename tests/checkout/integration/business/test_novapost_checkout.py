"""Checkout with Nova Post as the carrier (phase P2) — the money path.

Runs against the stub carrier. What is pinned here is everything that decides
what a customer is charged and what the shop can fulfil afterwards:

* the price comes from the carrier, never from the client;
* a free-delivery threshold short-circuits the carrier call;
* an unreachable carrier creates no order at all (fail-closed) while the shop's
  own methods keep working;
* the destination is snapshotted onto the order, so it stays readable later;
* the waybill number is withheld from the guest lookup.
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.order import Order, OrderDeliveryNovaPost

pytestmark = pytest.mark.asyncio

_QUOTE = "/api/v1/checkout/quote"
_CHECKOUT = "/api/v1/checkout"
_CART_ITEMS = "/api/v1/cart/items"

_CONTACT = {"email": "buyer@example.com", "phone": "+37360000000"}
_BRANCH = {
    "delivery_service": "novapost",
    "delivery_type": "branch",
    "np_division_id": "d-1",
    "np_recipient_name": "Ion Client",
}
_NP_COURIER = {
    "delivery_service": "novapost",
    "delivery_type": "courier",
    "np_settlement_id": "s-1",
    "np_recipient_name": "Ion Client",
    "np_address": {
        "city": "Chișinău",
        "street": "str. Testului",
        "building": "10",
        "postCode": "MD2000",
    },
}


@pytest.fixture(autouse=True)
def carrier_on(monkeypatch):
    """Serve every test in this module through the stub carrier."""
    monkeypatch.setattr(settings, "novapost_mode", "stub")
    monkeypatch.setattr(settings, "free_delivery_from", None)
    monkeypatch.setattr(settings, "novapost_free_delivery_from", None)


async def _seed_line(
    client: AsyncClient,
    add_product,
    *,
    product_id: int = 1,
    price: str = "100.00",
    qty: int = 1,
) -> None:
    """Insert a product and add it to the caller's cart."""
    await add_product(product_id, price=Decimal(price), code=f"NP-{product_id}", qty=50)
    resp = await client.post(_CART_ITEMS, json={"product_id": product_id, "qty": qty})
    assert resp.status_code == 201, resp.text


async def test_quote_prices_a_branch_shipment(client, add_product) -> None:
    """A pickup-point quote carries a carrier price and the chosen method."""
    await _seed_line(client, add_product)

    resp = await client.post(_QUOTE, json=_BRANCH)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delivery_service"] == "novapost"
    assert body["delivery_type"] == "branch"
    assert Decimal(body["delivery_cost"]) > 0
    assert Decimal(body["total"]) == Decimal(body["subtotal"]) + Decimal(
        body["delivery_cost"]
    )


async def test_courier_costs_more_than_a_pickup_point(client, add_product) -> None:
    """Door delivery is dearer than a branch — the carrier's tariff, not ours."""
    await _seed_line(client, add_product)

    branch = await client.post(_QUOTE, json=_BRANCH)
    courier = await client.post(_QUOTE, json=_NP_COURIER)

    assert Decimal(courier.json()["delivery_cost"]) > Decimal(
        branch.json()["delivery_cost"]
    )


async def test_heavier_cart_costs_more(client, add_product, db_session) -> None:
    """The parcel weight reaches the carrier: more units, higher price."""
    await _seed_line(client, add_product, product_id=1, qty=1)
    light = await client.post(_QUOTE, json=_BRANCH)

    await client.post(_CART_ITEMS, json={"product_id": 1, "qty": 9})
    heavy = await client.post(_QUOTE, json=_BRANCH)

    assert Decimal(heavy.json()["delivery_cost"]) > Decimal(
        light.json()["delivery_cost"]
    )


async def test_threshold_makes_carrier_delivery_free(
    client, add_product, monkeypatch
) -> None:
    """Above the carrier's own threshold the shipment is free and unquoted."""
    monkeypatch.setattr(settings, "novapost_free_delivery_from", Decimal("500"))
    await _seed_line(client, add_product, price="600.00")

    resp = await client.post(_QUOTE, json=_BRANCH)

    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["delivery_cost"]) == Decimal("0")


async def test_client_supplied_price_is_ignored(client, add_product) -> None:
    """A price in the request body cannot influence what is charged."""
    await _seed_line(client, add_product)

    honest = await client.post(_QUOTE, json=_BRANCH)
    spoofed = await client.post(
        _QUOTE, json={**_BRANCH, "delivery_cost": "0", "total": "1"}
    )

    assert spoofed.json()["delivery_cost"] == honest.json()["delivery_cost"]
    assert Decimal(spoofed.json()["delivery_cost"]) > 0


async def test_branch_without_a_pickup_point_is_422(client, add_product) -> None:
    """A carrier method needs its destination before anything is priced."""
    await _seed_line(client, add_product)

    resp = await client.post(
        _QUOTE,
        json={
            "delivery_service": "novapost",
            "delivery_type": "branch",
            "np_recipient_name": "Ion",
        },
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "novapost_destination_required"


async def test_carrier_courier_without_address_is_422(client, add_product) -> None:
    """Door delivery needs a city and an address."""
    await _seed_line(client, add_product)

    resp = await client.post(
        _QUOTE,
        json={
            "delivery_service": "novapost",
            "delivery_type": "courier",
            "np_settlement_id": "s-1",
            "np_recipient_name": "Ion",
        },
    )

    assert resp.status_code == 422, resp.text


async def test_unknown_method_combination_is_422(client, add_product) -> None:
    """Our own logistics has no pickup points — the pair must be rejected."""
    await _seed_line(client, add_product)

    resp = await client.post(
        _QUOTE, json={"delivery_service": "own", "delivery_type": "branch"}
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "invalid_delivery_method"


async def test_carrier_method_refused_while_disabled(
    client, add_product, monkeypatch
) -> None:
    """With the integration off the carrier cannot be chosen at all."""
    monkeypatch.setattr(settings, "novapost_mode", "")
    await _seed_line(client, add_product)

    resp = await client.post(_QUOTE, json=_BRANCH)

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "novapost_disabled"


async def test_carrier_outage_creates_no_order(
    client, add_product, db_session: AsyncSession, monkeypatch
) -> None:
    """Fail-closed: an unpriceable shipment must not become an order.

    The alternative — inventing a fallback price — sells delivery at a number
    the carrier never agreed to.
    """
    from app.services.delivery import novapost_stub
    from app.services.delivery.novapost_client import NovaPostError

    async def boom(self, recipient, parcels):
        raise NovaPostError("carrier down")

    monkeypatch.setattr(novapost_stub.NovaPostStub, "calculate", boom)
    await _seed_line(client, add_product)

    quote = await client.post(_QUOTE, json=_BRANCH)
    checkout = await client.post(_CHECKOUT, json={**_CONTACT, **_BRANCH})

    assert quote.status_code == 502, quote.text
    assert quote.json()["error"]["code"] == "delivery_quote_unavailable"
    assert checkout.status_code == 502, checkout.text
    orders = (await db_session.execute(select(Order))).scalars().all()
    assert orders == []


async def test_own_methods_survive_a_carrier_outage(
    client, add_product, monkeypatch
) -> None:
    """The shop keeps selling when the carrier is down — pickup is unaffected."""
    from app.services.delivery import novapost_stub
    from app.services.delivery.novapost_client import NovaPostError

    async def boom(self, recipient, parcels):
        raise NovaPostError("carrier down")

    monkeypatch.setattr(novapost_stub.NovaPostStub, "calculate", boom)
    await _seed_line(client, add_product)

    resp = await client.post(_QUOTE, json={"delivery_type": "pickup"})

    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["delivery_cost"]) == Decimal("0")


async def test_checkout_persists_the_carrier_destination(
    client, add_product, db_session: AsyncSession
) -> None:
    """The order carries a readable snapshot of where the parcel goes."""
    await _seed_line(client, add_product)

    resp = await client.post(_CHECKOUT, json={**_CONTACT, **_BRANCH})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["delivery_service"] == "novapost"
    assert body["novapost"]["division_number"] == "1"
    assert body["novapost"]["settlement_name"] == "Chișinău"

    order = (
        await db_session.execute(select(Order).where(Order.number == body["number"]))
    ).scalar_one()
    row = (
        await db_session.execute(
            select(OrderDeliveryNovaPost).where(
                OrderDeliveryNovaPost.order_id == order.id
            )
        )
    ).scalar_one()
    assert row.division_id == "d-1"
    assert row.division_address == "str. Ștefan cel Mare 12"
    # The Russian snapshot is taken too: the back-office reads orders in ru.
    assert row.settlement_name_ru == "Chișinău"
    assert row.calculated_cost == order.delivery_cost
    assert order.delivery_type == "branch"


async def test_checkout_persists_the_courier_address(
    client, add_product, db_session: AsyncSession
) -> None:
    """A door delivery stores the address in the carrier's own field layout."""
    await _seed_line(client, add_product)

    resp = await client.post(_CHECKOUT, json={**_CONTACT, **_NP_COURIER})

    assert resp.status_code == 201, resp.text
    order = (
        await db_session.execute(
            select(Order).where(Order.number == resp.json()["number"])
        )
    ).scalar_one()
    row = (
        await db_session.execute(
            select(OrderDeliveryNovaPost).where(
                OrderDeliveryNovaPost.order_id == order.id
            )
        )
    ).scalar_one()
    assert row.address_parts["street"] == "str. Testului"
    assert row.address_parts["building"] == "10"
    # Own-logistics address columns stay empty — the carrier holds this address.
    assert order.delivery_city is None


async def test_free_shipment_records_no_carrier_price(
    client, add_product, db_session: AsyncSession, monkeypatch
) -> None:
    """When the threshold applies we never asked for a price, so none is stored.

    Recording 0 would claim the shipment costs the shop nothing, which is the
    opposite of true.
    """
    monkeypatch.setattr(settings, "novapost_free_delivery_from", Decimal("500"))
    await _seed_line(client, add_product, price="600.00")

    resp = await client.post(_CHECKOUT, json={**_CONTACT, **_BRANCH})

    assert resp.status_code == 201, resp.text
    order = (
        await db_session.execute(
            select(Order).where(Order.number == resp.json()["number"])
        )
    ).scalar_one()
    row = (
        await db_session.execute(
            select(OrderDeliveryNovaPost).where(
                OrderDeliveryNovaPost.order_id == order.id
            )
        )
    ).scalar_one()
    assert order.delivery_cost == Decimal("0")
    assert row.calculated_cost is None


async def test_guest_lookup_hides_the_waybill(
    client, add_product, db_session: AsyncSession
) -> None:
    """Order number + email must not hand out a parcel tracking key."""
    await _seed_line(client, add_product)
    created = await client.post(_CHECKOUT, json={**_CONTACT, **_BRANCH})
    number = created.json()["number"]
    order = (
        await db_session.execute(select(Order).where(Order.number == number))
    ).scalar_one()
    row = (
        await db_session.execute(
            select(OrderDeliveryNovaPost).where(
                OrderDeliveryNovaPost.order_id == order.id
            )
        )
    ).scalar_one()
    row.awb_number = "AWB-SECRET"
    await db_session.flush()

    resp = await client.post(
        f"/api/v1/orders/{number}/lookup", json={"email": _CONTACT["email"]}
    )

    assert resp.status_code == 200, resp.text
    carrier = resp.json()["novapost"]
    assert carrier["division_number"] == "1"
    assert carrier["awb_number"] is None


async def test_own_order_has_no_carrier_block(client, add_product) -> None:
    """A pickup order reports no carrier data at all (contract stays clean)."""
    await _seed_line(client, add_product)

    resp = await client.post(_CHECKOUT, json={**_CONTACT, "delivery_type": "pickup"})

    assert resp.status_code == 201, resp.text
    assert resp.json()["novapost"] is None
    assert resp.json()["delivery_service"] == "own"


async def test_recipient_name_is_required(client, add_product) -> None:
    """A waybill has to name whoever collects the parcel."""
    await _seed_line(client, add_product)

    resp = await client.post(
        _QUOTE,
        json={
            "delivery_service": "novapost",
            "delivery_type": "branch",
            "np_division_id": "d-1",
        },
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "novapost_recipient_required"


async def test_recipient_name_is_stored_on_the_order(
    client, add_product, db_session: AsyncSession
) -> None:
    """The name lands in the order's delivery_name — the waybill reads it later."""
    await _seed_line(client, add_product)

    resp = await client.post(_CHECKOUT, json={**_CONTACT, **_BRANCH})

    assert resp.status_code == 201, resp.text
    order = (
        await db_session.execute(
            select(Order).where(Order.number == resp.json()["number"])
        )
    ).scalar_one()
    assert order.delivery_name == "Ion Client"


async def test_parcel_weight_is_snapshotted(
    client, add_product, db_session: AsyncSession
) -> None:
    """The quoted weight is kept so a later waybill declares the same parcel."""
    await _seed_line(client, add_product, qty=3)

    resp = await client.post(_CHECKOUT, json={**_CONTACT, **_BRANCH})

    order = (
        await db_session.execute(
            select(Order).where(Order.number == resp.json()["number"])
        )
    ).scalar_one()
    row = (
        await db_session.execute(
            select(OrderDeliveryNovaPost).where(
                OrderDeliveryNovaPost.order_id == order.id
            )
        )
    ).scalar_one()
    assert row.parcel_weight_g == 3 * 500  # default per-item weight
