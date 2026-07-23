"""Order list auth, stock shortage 409, and authenticated-user checkout (B6).

Contract:
* ``GET /orders`` requires ``current_user`` — 401 without a token, list of
  ``OrderOut`` with one.
* Checkout with insufficient stock → 409 with error ``code == "out_of_stock"``.
* Authenticated checkout attaches ``user_id`` (not null) and the order shows up
  in ``GET /orders``.
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order

pytestmark = pytest.mark.asyncio


async def _seed_guest_cart(
    client: AsyncClient,
    add_product,
    *,
    product_id: int,
    price: Decimal,
    qty: int,
    stock: int = 1000,
) -> None:
    """Create a product with ``stock`` on hand and add ``qty`` to the guest cart."""
    await add_product(product_id, price=price, code=f"P{product_id}", qty=stock)
    resp = await client.post(
        "/api/v1/cart/items",
        json={"product_id": product_id, "qty": qty},
    )
    assert resp.status_code == 201, resp.text
    client.cookies.set("session_token", resp.cookies["session_token"])


async def _seed_user_cart(
    user_client: AsyncClient,
    add_product,
    *,
    product_id: int,
    price: Decimal,
    qty: int,
    stock: int = 1000,
) -> None:
    """Create a product and add ``qty`` to the authenticated user's cart."""
    await add_product(product_id, price=price, code=f"P{product_id}", qty=stock)
    resp = await user_client.post(
        "/api/v1/cart/items",
        json={"product_id": product_id, "qty": qty},
    )
    assert resp.status_code == 201, resp.text


async def test_orders_list_requires_auth(client: AsyncClient) -> None:
    """``GET /orders`` without a token → 401 (contract)."""
    resp = await client.get("/api/v1/orders")
    assert resp.status_code == 401, resp.text


async def test_out_of_stock_checkout_is_409(
    client: AsyncClient,
    add_product,
) -> None:
    """Ordering more than stock on hand → 409 ``out_of_stock`` (contract code)."""
    # Stock = 1 but the cart wants 2.
    await _seed_guest_cart(
        client, add_product, product_id=1, price=Decimal("100.00"), qty=2, stock=1
    )

    resp = await client.post(
        "/api/v1/checkout",
        json={
            "email": "oos@example.com",
            "phone": "+37360000006",
            "delivery_type": "pickup",
        },
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "out_of_stock"


async def test_out_of_stock_does_not_decrement_stock(
    client: AsyncClient,
    add_product,
    db_session: AsyncSession,
) -> None:
    """A rejected (409) checkout must not touch ``product.qty`` (atomicity)."""
    product = await _seed_guest_cart_returning(
        client, add_product, product_id=1, price=Decimal("100.00"), qty=5, stock=3
    )

    resp = await client.post(
        "/api/v1/checkout",
        json={
            "email": "oos2@example.com",
            "phone": "+37360000007",
            "delivery_type": "pickup",
        },
    )
    assert resp.status_code == 409, resp.text

    await db_session.refresh(product)
    assert product.qty == 3


async def _seed_guest_cart_returning(
    client: AsyncClient,
    add_product,
    *,
    product_id: int,
    price: Decimal,
    qty: int,
    stock: int,
):
    """Same as ``_seed_guest_cart`` but returns the created product."""
    product = await add_product(
        product_id, price=price, code=f"P{product_id}", qty=stock
    )
    resp = await client.post(
        "/api/v1/cart/items",
        json={"product_id": product_id, "qty": qty},
    )
    assert resp.status_code == 201, resp.text
    client.cookies.set("session_token", resp.cookies["session_token"])
    return product


async def test_user_checkout_attaches_user_and_lists(
    user_client: AsyncClient,
    add_product,
    db_session: AsyncSession,
) -> None:
    """Authenticated checkout sets ``user_id`` and appears in ``GET /orders``."""
    await _seed_user_cart(
        user_client, add_product, product_id=1, price=Decimal("100.00"), qty=1
    )

    created = await user_client.post(
        "/api/v1/checkout",
        json={
            "email": "member@example.com",
            "phone": "+37360000008",
            "delivery_type": "pickup",
        },
    )
    assert created.status_code == 201, created.text
    number = created.json()["number"]

    order = (
        await db_session.execute(select(Order).where(Order.number == number))
    ).scalar_one()
    assert order.user_id is not None

    listing = await user_client.get("/api/v1/orders")
    assert listing.status_code == 200, listing.text
    numbers = [item["number"] for item in listing.json()]
    assert number in numbers


async def test_stock_decrement_on_success(
    client: AsyncClient,
    add_product,
    db_session: AsyncSession,
) -> None:
    """A successful checkout decrements ``product.qty`` by the ordered amount."""
    product = await _seed_guest_cart_returning(
        client, add_product, product_id=1, price=Decimal("100.00"), qty=4, stock=10
    )

    resp = await client.post(
        "/api/v1/checkout",
        json={
            "email": "dec@example.com",
            "phone": "+37360000009",
            "delivery_type": "pickup",
        },
    )
    assert resp.status_code == 201, resp.text

    await db_session.refresh(product)
    assert product.qty == 6
