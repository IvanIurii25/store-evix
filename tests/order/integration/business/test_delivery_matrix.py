"""Delivery-cost matrix + empty-cart guards via ``POST /checkout/quote`` (B6).

Contract (§9): ``pickup`` → 0; ``courier`` → ``settings.courier_rate``;
``courier`` with ``subtotal >= settings.free_delivery_from`` → 0; ``courier``
without an address → 422. An empty cart → 400 on both quote and checkout.

These are adversarial: they pin the exact ``delivery_cost`` and the arithmetic
``total = subtotal - discount + delivery`` so an implementation that, e.g.,
charges courier on the free-delivery threshold, or forgets to add delivery into
``total``, fails here.
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient

from app.core.config import settings

pytestmark = pytest.mark.asyncio


async def _seed_guest_cart(
    client: AsyncClient,
    add_product,
    *,
    product_id: int,
    price: Decimal,
    qty: int,
) -> None:
    """Add a product to the guest cart over HTTP and pin the session cookie.

    Args:
        client: The guest HTTP client.
        add_product: Product-insertion helper fixture.
        product_id: Catalog product id to create + add.
        price: Unit price.
        qty: Quantity to add to the cart.
    """
    await add_product(product_id, price=price, code=f"P{product_id}", qty=1000)
    resp = await client.post(
        "/api/v1/cart/items",
        json={"product_id": product_id, "qty": qty},
    )
    assert resp.status_code == 201, resp.text
    client.cookies.set("session_token", resp.cookies["session_token"])


async def test_pickup_delivery_is_zero(client: AsyncClient, add_product) -> None:
    """``pickup`` quote returns ``delivery_cost = 0`` and matching total."""
    await _seed_guest_cart(
        client, add_product, product_id=1, price=Decimal("100.00"), qty=2
    )

    resp = await client.post("/api/v1/checkout/quote", json={"delivery_type": "pickup"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delivery_type"] == "pickup"
    assert Decimal(body["subtotal"]) == Decimal("200.00")
    assert Decimal(body["delivery_cost"]) == Decimal("0")
    assert Decimal(body["discount_total"]) == Decimal("0")
    assert Decimal(body["total"]) == Decimal("200.00")
    assert body["item_count"] == 2


async def test_courier_delivery_charges_courier_rate(
    client: AsyncClient,
    add_product,
    monkeypatch,
) -> None:
    """``courier`` below the free threshold charges exactly ``courier_rate``."""
    monkeypatch.setattr(settings, "courier_rate", Decimal("50"))
    monkeypatch.setattr(settings, "free_delivery_from", None)
    await _seed_guest_cart(
        client, add_product, product_id=1, price=Decimal("100.00"), qty=1
    )

    resp = await client.post(
        "/api/v1/checkout/quote",
        json={"delivery_type": "courier", "delivery_address_id": 1},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delivery_type"] == "courier"
    assert Decimal(body["subtotal"]) == Decimal("100.00")
    assert Decimal(body["delivery_cost"]) == Decimal("50")
    assert Decimal(body["total"]) == Decimal("150.00")


async def test_courier_free_over_threshold(
    client: AsyncClient,
    add_product,
    monkeypatch,
) -> None:
    """``courier`` with ``subtotal >= free_delivery_from`` → delivery is free."""
    monkeypatch.setattr(settings, "courier_rate", Decimal("50"))
    monkeypatch.setattr(settings, "free_delivery_from", Decimal("500"))
    # subtotal = 600 >= 500 → free.
    await _seed_guest_cart(
        client, add_product, product_id=1, price=Decimal("600.00"), qty=1
    )

    resp = await client.post(
        "/api/v1/checkout/quote",
        json={"delivery_type": "courier", "delivery_address_id": 1},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert Decimal(body["subtotal"]) == Decimal("600.00")
    assert Decimal(body["delivery_cost"]) == Decimal("0")
    assert Decimal(body["total"]) == Decimal("600.00")


async def test_courier_exactly_at_threshold_is_free(
    client: AsyncClient,
    add_product,
    monkeypatch,
) -> None:
    """Boundary: ``subtotal == free_delivery_from`` is free (>=, not >)."""
    monkeypatch.setattr(settings, "courier_rate", Decimal("50"))
    monkeypatch.setattr(settings, "free_delivery_from", Decimal("500"))
    await _seed_guest_cart(
        client, add_product, product_id=1, price=Decimal("500.00"), qty=1
    )

    resp = await client.post(
        "/api/v1/checkout/quote",
        json={"delivery_type": "courier", "delivery_address_id": 1},
    )

    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["delivery_cost"]) == Decimal("0")


async def test_courier_just_below_threshold_charged(
    client: AsyncClient,
    add_product,
    monkeypatch,
) -> None:
    """Boundary: ``subtotal`` one cent below the threshold still pays courier."""
    monkeypatch.setattr(settings, "courier_rate", Decimal("50"))
    monkeypatch.setattr(settings, "free_delivery_from", Decimal("500"))
    await _seed_guest_cart(
        client, add_product, product_id=1, price=Decimal("499.99"), qty=1
    )

    resp = await client.post(
        "/api/v1/checkout/quote",
        json={"delivery_type": "courier", "delivery_address_id": 1},
    )

    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["delivery_cost"]) == Decimal("50")


async def test_courier_without_address_is_422(
    client: AsyncClient,
    add_product,
) -> None:
    """``courier`` with no ``delivery_address_id`` → 422 (contract)."""
    await _seed_guest_cart(
        client, add_product, product_id=1, price=Decimal("100.00"), qty=1
    )

    resp = await client.post(
        "/api/v1/checkout/quote", json={"delivery_type": "courier"}
    )

    assert resp.status_code == 422, resp.text


async def test_empty_cart_quote_is_400(client: AsyncClient) -> None:
    """Quoting an empty cart → 400 (contract)."""
    resp = await client.post("/api/v1/checkout/quote", json={"delivery_type": "pickup"})
    assert resp.status_code == 400, resp.text


async def test_empty_cart_checkout_is_400(client: AsyncClient) -> None:
    """Checking out an empty cart → 400 (contract)."""
    resp = await client.post(
        "/api/v1/checkout",
        json={
            "email": "guest@example.com",
            "phone": "+37360000000",
            "delivery_type": "pickup",
        },
    )
    assert resp.status_code == 400, resp.text
