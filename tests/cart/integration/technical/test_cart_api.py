"""API tests for the cart domain (B5 DoD).

Covers: guest add (cart without user), repeat-adds increment (no duplicate row),
live totals from ``product.price``, guest→user merge (qty summed), a
deactivated/removed product not breaking ``GET /cart``, and language-aware line
names with a default-language fallback (``?lang=``).
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cart import Cart, CartItem
from app.models.catalog import ProductTranslation

pytestmark = pytest.mark.asyncio


async def _add_ru_translation(
    db_session: AsyncSession,
    product_id: int,
    *,
    name: str,
) -> None:
    """Add a ``ru`` translation for an existing product (``add_product`` seeds ``ro``)."""
    db_session.add(
        ProductTranslation(
            product_id=product_id,
            lang="ru",
            name=name,
            slug=f"slug-ru-{product_id}",
        )
    )
    await db_session.flush()


async def test_guest_add_creates_userless_cart(
    client: AsyncClient,
    db_session: AsyncSession,
    add_product,
) -> None:
    """A guest ``POST /cart/items`` creates a cart with no user + sets the cookie."""
    await add_product(1, price=Decimal("100.00"), code="P1")

    resp = await client.post("/api/v1/cart/items", json={"product_id": 1, "qty": 2})

    assert resp.status_code == 201
    body = resp.json()
    assert body["item_count"] == 2
    assert body["subtotal"] == "200.00"
    assert "session_token" in resp.cookies

    carts = (await db_session.execute(select(Cart))).scalars().all()
    assert len(carts) == 1
    assert carts[0].user_id is None
    assert carts[0].session_token is not None


async def test_repeat_add_increments_without_duplicate_row(
    client: AsyncClient,
    db_session: AsyncSession,
    add_product,
) -> None:
    """Adding the same product twice increments qty on one row (unique constraint)."""
    await add_product(1, price=Decimal("50.00"), code="P1")

    first = await client.post("/api/v1/cart/items", json={"product_id": 1, "qty": 1})
    token = first.cookies["session_token"]
    client.cookies.set("session_token", token)
    second = await client.post("/api/v1/cart/items", json={"product_id": 1, "qty": 3})

    assert second.status_code == 201
    body = second.json()
    assert body["item_count"] == 4
    assert len(body["items"]) == 1

    row_count = (
        await db_session.execute(select(func.count()).select_from(CartItem))
    ).scalar_one()
    assert row_count == 1


async def test_subtotal_recomputed_from_product_price(
    client: AsyncClient,
    db_session: AsyncSession,
    add_product,
) -> None:
    """Totals reflect the live ``product.price`` (not stored in the cart)."""
    product = await add_product(1, price=Decimal("10.00"), code="P1")
    first = await client.post("/api/v1/cart/items", json={"product_id": 1, "qty": 3})
    client.cookies.set("session_token", first.cookies["session_token"])

    # Price changes on the product; cart must re-read it.
    product.price = Decimal("25.00")
    await db_session.flush()

    resp = await client.get("/api/v1/cart")

    body = resp.json()
    assert body["items"][0]["price"] == "25.00"
    assert body["items"][0]["line_total"] == "75.00"
    assert body["subtotal"] == "75.00"


async def test_merge_sums_matching_quantities(
    user_client: AsyncClient,
    db_session: AsyncSession,
    add_product,
) -> None:
    """Merge folds the guest cart into the user cart, summing matching qty (§7)."""
    await add_product(1, price=Decimal("10.00"), code="P1")
    await add_product(2, price=Decimal("20.00"), code="P2")

    guest_token = uuid4()
    guest_cart = Cart(user_id=None, session_token=guest_token, status="draft")
    db_session.add(guest_cart)
    user_cart = Cart(user_id=9001, session_token=None, status="draft")
    db_session.add(user_cart)
    await db_session.flush()
    # Guest has 2x P1 + 5x P2; user already has 3x P1.
    db_session.add(CartItem(cart_id=guest_cart.id, product_id=1, qty=2))
    db_session.add(CartItem(cart_id=guest_cart.id, product_id=2, qty=5))
    db_session.add(CartItem(cart_id=user_cart.id, product_id=1, qty=3))
    await db_session.flush()

    user_client.cookies.set("session_token", str(guest_token))
    resp = await user_client.post("/api/v1/cart/merge")

    assert resp.status_code == 200
    body = resp.json()
    by_id = {item["product_id"]: item for item in body["items"]}
    assert by_id[1]["qty"] == 5  # 3 (user) + 2 (guest)
    assert by_id[2]["qty"] == 5  # guest-only, moved
    assert body["item_count"] == 10

    # Guest cart is gone; user cart owns both lines.
    remaining = (await db_session.execute(select(Cart))).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].user_id == 9001


async def test_inactive_product_does_not_break_get_cart(
    client: AsyncClient,
    db_session: AsyncSession,
    add_product,
) -> None:
    """A line whose product was deactivated is skipped, not fatal, on GET /cart."""
    active = await add_product(1, price=Decimal("10.00"), code="P1")
    inactive = await add_product(2, price=Decimal("99.00"), code="P2")

    first = await client.post("/api/v1/cart/items", json={"product_id": 1, "qty": 1})
    client.cookies.set("session_token", first.cookies["session_token"])
    await client.post("/api/v1/cart/items", json={"product_id": 2, "qty": 1})

    # Deactivate the second product after it is in the cart.
    inactive.is_active = False
    await db_session.flush()

    resp = await client.get("/api/v1/cart")

    assert resp.status_code == 200
    body = resp.json()
    ids = [item["product_id"] for item in body["items"]]
    assert ids == [1]
    assert body["subtotal"] == "10.00"
    assert active.is_active is True


async def test_add_inactive_product_rejected(
    client: AsyncClient,
    add_product,
) -> None:
    """Adding an inactive product returns 404 in the unified error envelope."""
    await add_product(1, price=Decimal("10.00"), code="P1", is_active=False)

    resp = await client.post("/api/v1/cart/items", json={"product_id": 1, "qty": 1})

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "http_error"


async def test_cart_name_is_language_aware(
    client: AsyncClient,
    db_session: AsyncSession,
    add_product,
) -> None:
    """``GET /cart`` returns the RU name for ``?lang=ru`` and RO otherwise."""
    await add_product(1, price=Decimal("10.00"), code="P1", name="Produs RO")
    await _add_ru_translation(db_session, 1, name="Товар RU")

    first = await client.post("/api/v1/cart/items", json={"product_id": 1, "qty": 1})
    client.cookies.set("session_token", first.cookies["session_token"])

    ru = await client.get("/api/v1/cart", params={"lang": "ru"})
    ro = await client.get("/api/v1/cart", params={"lang": "ro"})
    default = await client.get("/api/v1/cart")

    assert ru.json()["items"][0]["name"] == "Товар RU"
    assert ro.json()["items"][0]["name"] == "Produs RO"
    # No ?lang= defaults to ro (§2.6).
    assert default.json()["items"][0]["name"] == "Produs RO"


async def test_cart_name_falls_back_to_default_lang(
    client: AsyncClient,
    add_product,
) -> None:
    """A product lacking the requested lang falls back to the RO name, not the code."""
    # ``add_product`` seeds only the ``ro`` translation; no ``ru`` row exists.
    await add_product(1, price=Decimal("10.00"), code="P1", name="Produs RO")

    first = await client.post("/api/v1/cart/items", json={"product_id": 1, "qty": 1})
    client.cookies.set("session_token", first.cookies["session_token"])

    resp = await client.get("/api/v1/cart", params={"lang": "ru"})

    # Coalesce: requested ru missing -> default ro name (never the bare code).
    name = resp.json()["items"][0]["name"]
    assert name == "Produs RO"
    assert name != "P1"
