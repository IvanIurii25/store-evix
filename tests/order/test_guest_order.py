"""Guest checkout, order lookup, COD fields, number format, snapshot immutability.

Contract (§9, OrderOut):
* Guest checkout (no token): ``email`` + ``phone`` required, order ``user_id``
  null; lookup by ``GET /orders/{number}?email=`` — wrong email → 404.
* COD: ``payment_method='cod'``, ``payment_status='pending'``, ``status='new'``;
  ``number`` matches ``^\\d{8}-\\d{6}$``.
* Line snapshots are immutable: mutating ``product.price`` / name afterwards does
  not change the persisted ``price_snapshot`` / ``name_snapshot``.
"""

import re
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import ProductTranslation
from app.models.order import Order

pytestmark = pytest.mark.asyncio

_NUMBER_RE = re.compile(r"^\d{8}-\d{6}$")


async def _seed_guest_cart(
    client: AsyncClient,
    add_product,
    *,
    product_id: int,
    price: Decimal,
    qty: int,
    name: str = "Product",
) -> None:
    """Create a product and add it to the guest cart over HTTP (pins cookie)."""
    await add_product(
        product_id, price=price, code=f"P{product_id}", qty=1000, name=name
    )
    resp = await client.post(
        "/api/v1/cart/items",
        json={"product_id": product_id, "qty": qty},
    )
    assert resp.status_code == 201, resp.text
    client.cookies.set("session_token", resp.cookies["session_token"])


async def test_guest_checkout_creates_userless_order(
    client: AsyncClient,
    add_product,
    db_session: AsyncSession,
) -> None:
    """Guest checkout without a token yields a 201 order with ``user_id`` null."""
    await _seed_guest_cart(
        client, add_product, product_id=1, price=Decimal("100.00"), qty=2
    )

    resp = await client.post(
        "/api/v1/checkout",
        json={
            "email": "guest@example.com",
            "phone": "+37360000001",
            "delivery_type": "pickup",
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == "guest@example.com"
    assert body["phone"] == "+37360000001"
    assert Decimal(body["total"]) == Decimal("200.00")

    order = (
        await db_session.execute(select(Order).where(Order.number == body["number"]))
    ).scalar_one()
    assert order.user_id is None


async def test_cod_fields_and_number_format(
    client: AsyncClient,
    add_product,
) -> None:
    """COD invariants + ``number`` format ``YYYYMMDD-<6 digits>``."""
    await _seed_guest_cart(
        client, add_product, product_id=1, price=Decimal("100.00"), qty=1
    )

    resp = await client.post(
        "/api/v1/checkout",
        json={
            "email": "cod@example.com",
            "phone": "+37360000002",
            "delivery_type": "pickup",
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["payment_method"] == "cod"
    assert body["payment_status"] == "pending"
    assert body["status"] == "new"
    assert _NUMBER_RE.match(body["number"]), body["number"]
    # No online-payment leakage into the DTO.
    assert "payment_url" not in body


async def test_guest_lookup_by_correct_email(
    client: AsyncClient,
    add_product,
) -> None:
    """Guest order lookup with the right email → 200; items are snapshotted."""
    await _seed_guest_cart(
        client,
        add_product,
        product_id=1,
        price=Decimal("100.00"),
        qty=3,
        name="Widget",
    )
    created = await client.post(
        "/api/v1/checkout",
        json={
            "email": "buyer@example.com",
            "phone": "+37360000003",
            "delivery_type": "pickup",
        },
    )
    number = created.json()["number"]

    resp = await client.get(
        f"/api/v1/orders/{number}", params={"email": "buyer@example.com"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["number"] == number
    assert len(body["items"]) == 1
    line = body["items"][0]
    assert line["name_snapshot"] == "Widget"
    assert Decimal(line["price_snapshot"]) == Decimal("100.00")
    assert line["qty"] == 3


async def test_guest_lookup_wrong_email_is_404(
    client: AsyncClient,
    add_product,
) -> None:
    """Guest order lookup with a wrong email → 404 (no cross-customer leak)."""
    await _seed_guest_cart(
        client, add_product, product_id=1, price=Decimal("100.00"), qty=1
    )
    created = await client.post(
        "/api/v1/checkout",
        json={
            "email": "owner@example.com",
            "phone": "+37360000004",
            "delivery_type": "pickup",
        },
    )
    number = created.json()["number"]

    resp = await client.get(
        f"/api/v1/orders/{number}", params={"email": "attacker@example.com"}
    )

    assert resp.status_code == 404, resp.text


async def _add_ru_translation(
    db_session: AsyncSession,
    product_id: int,
    *,
    name: str,
) -> None:
    """Add a ``ru`` translation for a product (``add_product`` seeds only ``ro``)."""
    db_session.add(
        ProductTranslation(
            product_id=product_id,
            lang="ru",
            name=name,
            slug=f"slug-ru-{product_id}",
        )
    )
    await db_session.flush()


async def test_order_snapshot_uses_requested_language(
    client: AsyncClient,
    add_product,
    db_session: AsyncSession,
) -> None:
    """Checkout with ``?lang=ru`` snapshots the RU line name; ``ro`` snapshots RO."""
    await _seed_guest_cart(
        client, add_product, product_id=1, price=Decimal("100.00"), qty=1, name="Produs RO"
    )
    await _add_ru_translation(db_session, 1, name="Товар RU")

    ru_order = await client.post(
        "/api/v1/checkout",
        params={"lang": "ru"},
        json={
            "email": "ru@example.com",
            "phone": "+37360000010",
            "delivery_type": "pickup",
        },
    )

    assert ru_order.status_code == 201, ru_order.text
    assert ru_order.json()["items"][0]["name_snapshot"] == "Товар RU"


async def test_order_snapshot_defaults_to_ro(
    client: AsyncClient,
    add_product,
    db_session: AsyncSession,
) -> None:
    """Checkout without ``?lang=`` snapshots the default (RO) line name (§2.6)."""
    await _seed_guest_cart(
        client, add_product, product_id=1, price=Decimal("100.00"), qty=1, name="Produs RO"
    )
    await _add_ru_translation(db_session, 1, name="Товар RU")

    ro_order = await client.post(
        "/api/v1/checkout",
        json={
            "email": "ro@example.com",
            "phone": "+37360000011",
            "delivery_type": "pickup",
        },
    )

    assert ro_order.status_code == 201, ro_order.text
    assert ro_order.json()["items"][0]["name_snapshot"] == "Produs RO"


async def test_snapshot_immutable_after_price_and_name_change(
    client: AsyncClient,
    add_product,
    db_session: AsyncSession,
) -> None:
    """Mutating product price/name after order keeps the order snapshot intact."""
    product = await add_product(
        1, price=Decimal("100.00"), code="P1", qty=1000, name="OldName"
    )
    add_resp = await client.post("/api/v1/cart/items", json={"product_id": 1, "qty": 2})
    client.cookies.set("session_token", add_resp.cookies["session_token"])

    created = await client.post(
        "/api/v1/checkout",
        json={
            "email": "snap@example.com",
            "phone": "+37360000005",
            "delivery_type": "pickup",
        },
    )
    assert created.status_code == 201, created.text
    number = created.json()["number"]

    # Mutate the live product AFTER the order exists.
    product.price = Decimal("999.99")
    translation = (
        await db_session.execute(
            select(ProductTranslation).where(
                ProductTranslation.product_id == 1,
                ProductTranslation.lang == "ro",
            )
        )
    ).scalar_one()
    translation.name = "NewName"
    await db_session.flush()

    resp = await client.get(
        f"/api/v1/orders/{number}", params={"email": "snap@example.com"}
    )

    assert resp.status_code == 200, resp.text
    line = resp.json()["items"][0]
    assert Decimal(line["price_snapshot"]) == Decimal("100.00")
    assert line["name_snapshot"] == "OldName"
