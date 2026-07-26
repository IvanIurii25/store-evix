"""Business tests for promo codes on the checkout money-path (feature A1, §2.5).

Exercises the two load-bearing scenarios end-to-end through the real quote /
checkout endpoints on the commit-safe session:

* quote applies a valid percent / fixed discount and recomputes ``total``;
* checkout re-validates and persists ``promo_code`` + ``discount_total`` +
  discounted ``total`` onto the order;
* invalid / expired / min-order / usage-limit / clamp rules surface correctly;
* the no-promo path is byte-for-byte unchanged (regression guard).
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order

pytestmark = pytest.mark.asyncio

_CHECKOUT = "/api/v1/checkout"
_QUOTE = "/api/v1/checkout/quote"
_CART_ITEMS = "/api/v1/cart/items"

_GUEST_BODY = {
    "email": "buyer@example.com",
    "phone": "+37360000000",
    "delivery_type": "pickup",
}


async def _seed_line(
    client: AsyncClient,
    add_product,
    *,
    product_id: int,
    price: str,
    qty: int,
    stock: int = 100,
) -> None:
    """Insert a product then add a guest cart line (sets the cookie on ``client``)."""
    await add_product(
        product_id,
        price=Decimal(price),
        code=f"SKU-{product_id}",
        qty=stock,
    )
    resp = await client.post(_CART_ITEMS, json={"product_id": product_id, "qty": qty})
    assert resp.status_code == 201, resp.text


# --------------------------------------------------------------------------- #
# Quote — discount applied
# --------------------------------------------------------------------------- #
async def test_quote_percent_discount(client, add_product, add_promo) -> None:
    """A valid percent code reduces total by percent of subtotal (pickup)."""
    await _seed_line(client, add_product, product_id=1, price="100.00", qty=2)
    await add_promo("SAVE10", discount_type="percent", discount_value=Decimal("10"))

    resp = await client.post(
        _QUOTE, json={"delivery_type": "pickup", "promo_code": "SAVE10"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert Decimal(body["subtotal"]) == Decimal("200.00")
    assert Decimal(body["discount_total"]) == Decimal("20.00")
    assert Decimal(body["total"]) == Decimal("180.00")


async def test_quote_percent_discount_rounds_to_whole_mdl(
    client, add_product, add_promo
) -> None:
    """BUG-02: a fractional percent discount is quantized to whole MDL so the
    stored discount / total match what the whole-MDL front shows.

    10% of 249 is 24.90 raw; with courier (+50) the shown discount was 25 while
    the back stored 24.90 / total 274.10. Whole-MDL rounding makes both 25 / 274.
    """
    await _seed_line(client, add_product, product_id=1, price="249.00", qty=1)
    await add_promo("SAVE10", discount_type="percent", discount_value=Decimal("10"))

    resp = await client.post(
        _QUOTE,
        json={
            "delivery_type": "courier",
            "promo_code": "SAVE10",
            "delivery_address": {
                "full_name": "Ion Guest",
                "city": "Chișinău",
                "street": "str. Testului 1",
            },
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert Decimal(body["subtotal"]) == Decimal("249")
    assert Decimal(body["discount_total"]) == Decimal("25")  # 24.90 → 25
    assert Decimal(body["delivery_cost"]) == Decimal("50")
    # 249 - 25 + 50 = 274, whole MDL (was 274.10 before BUG-02 fix).
    assert Decimal(body["total"]) == Decimal("274")


async def test_quote_fixed_discount(client, add_product, add_promo) -> None:
    """A valid fixed code subtracts a flat amount from the subtotal."""
    await _seed_line(client, add_product, product_id=1, price="100.00", qty=1)
    await add_promo("MINUS30", discount_type="fixed", discount_value=Decimal("30"))

    resp = await client.post(
        _QUOTE, json={"delivery_type": "pickup", "promo_code": "MINUS30"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert Decimal(body["discount_total"]) == Decimal("30.00")
    assert Decimal(body["total"]) == Decimal("70.00")


async def test_quote_fixed_discount_clamped_to_subtotal(
    client, add_product, add_promo
) -> None:
    """A fixed discount larger than the subtotal is clamped (total never < 0)."""
    await _seed_line(client, add_product, product_id=1, price="40.00", qty=1)
    await add_promo("HUGE", discount_type="fixed", discount_value=Decimal("500"))

    resp = await client.post(
        _QUOTE, json={"delivery_type": "pickup", "promo_code": "HUGE"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert Decimal(body["discount_total"]) == Decimal("40.00")
    assert Decimal(body["total"]) == Decimal("0.00")


async def test_quote_unknown_code_404(client, add_product) -> None:
    """An unknown code is rejected with ``promo_invalid`` (404)."""
    await _seed_line(client, add_product, product_id=1, price="100.00", qty=1)

    resp = await client.post(
        _QUOTE, json={"delivery_type": "pickup", "promo_code": "NOPE"}
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "promo_invalid"


async def test_quote_expired_code_400(client, add_product, add_promo) -> None:
    """A code outside its validity window is rejected with ``promo_expired``."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    await _seed_line(client, add_product, product_id=1, price="100.00", qty=1)
    await add_promo(
        "OLD",
        active_from=now - timedelta(days=10),
        active_to=now - timedelta(days=5),
    )

    resp = await client.post(
        _QUOTE, json={"delivery_type": "pickup", "promo_code": "OLD"}
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "promo_expired"


async def test_quote_min_order_not_met_400(client, add_product, add_promo) -> None:
    """A code whose min-order exceeds the subtotal yields ``promo_min_order``."""
    await _seed_line(client, add_product, product_id=1, price="50.00", qty=1)
    await add_promo("BIG", min_order_total=Decimal("100"))

    resp = await client.post(
        _QUOTE, json={"delivery_type": "pickup", "promo_code": "BIG"}
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "promo_min_order"


# --------------------------------------------------------------------------- #
# Checkout — persistence
# --------------------------------------------------------------------------- #
async def test_checkout_persists_discount(
    client, add_product, add_promo, db_session: AsyncSession
) -> None:
    """Checkout persists promo_code + discount_total + discounted total."""
    await _seed_line(client, add_product, product_id=1, price="100.00", qty=2)
    await add_promo("SAVE10", discount_type="percent", discount_value=Decimal("10"))

    resp = await client.post(_CHECKOUT, json={**_GUEST_BODY, "promo_code": "SAVE10"})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert Decimal(body["discount_total"]) == Decimal("20.00")
    assert Decimal(body["total"]) == Decimal("180.00")

    order = (
        await db_session.execute(select(Order).where(Order.number == body["number"]))
    ).scalar_one()
    assert order.promo_code == "SAVE10"
    assert order.discount_total == Decimal("20.00")
    assert order.total == Decimal("180.00")


async def test_checkout_usage_limit_exhausted_409(
    client, add_product, add_promo
) -> None:
    """A code at its usage limit (after one redemption) is refused with 409."""
    await _seed_line(
        client, add_product, product_id=1, price="100.00", qty=1, stock=100
    )
    await add_promo("ONCE", usage_limit=1)

    first = await client.post(_CHECKOUT, json={**_GUEST_BODY, "promo_code": "ONCE"})
    assert first.status_code == 201, first.text

    # Second buyer (fresh guest, no cookie) seeds a new line and reuses the code.
    async with AsyncClient(
        transport=client._transport, base_url="http://test"
    ) as second:
        await _seed_line(second, add_product, product_id=2, price="100.00", qty=1)
        resp = await second.post(_CHECKOUT, json={**_GUEST_BODY, "promo_code": "ONCE"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "promo_usage_limit"


async def test_checkout_without_promo_unchanged(
    client, add_product, db_session: AsyncSession
) -> None:
    """The no-promo money-path is unchanged: zero discount, null code."""
    await _seed_line(client, add_product, product_id=1, price="100.00", qty=2)

    resp = await client.post(_CHECKOUT, json=_GUEST_BODY)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert Decimal(body["discount_total"]) == Decimal("0")
    assert Decimal(body["total"]) == Decimal("200.00")

    order = (
        await db_session.execute(select(Order).where(Order.number == body["number"]))
    ).scalar_one()
    assert order.promo_code is None
