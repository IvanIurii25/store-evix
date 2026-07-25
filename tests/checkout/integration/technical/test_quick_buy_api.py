"""API tests for one-click buy ``POST /checkout/quick`` (feature A2, §9).

Covers the phone-only happy path (COD, free pickup, single line, stock
decrement, placeholder email + name snapshot), an explicit-email variant, and
the error paths: out-of-stock → 409 ``out_of_stock``, unknown/inactive product
→ 404 ``product_not_found``, and invalid phone / qty → 422 (Pydantic).

Reuses the checkout ``client`` + ``add_product`` fixtures. Quick buy needs no
cart, so no ``_seed_line`` — the product is inserted directly.
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

_QUICK = "/api/v1/checkout/quick"


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
async def test_quick_buy_phone_only_happy_path(
    client: AsyncClient, add_product
) -> None:
    """Phone-only quick buy creates a COD pickup order with a placeholder email."""
    await add_product(1, price=Decimal("100.00"), code="Q-1", qty=5, name="Widget")

    resp = await client.post(
        _QUICK,
        json={"product_id": 1, "phone": "+373 60 000 111", "name": "Ion Client"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "new"
    assert body["payment_status"] == "pending"
    assert body["payment_method"] == "cod"
    assert body["delivery_type"] == "pickup"
    assert body["delivery_cost"] == "0"
    assert body["total"] == "100.00"
    assert body["subtotal"] == "100.00"
    assert body["phone"] == "+373 60 000 111"
    # Placeholder email is derived from the phone digits (email column NOT NULL).
    assert body["email"] == "37360000111@quick.evix.md"
    # Optional name is snapshotted onto delivery_name.
    assert body["delivery_name"] == "Ion Client"
    assert body["delivery_address_id"] is None
    assert body["number"]
    assert len(body["items"]) == 1
    line = body["items"][0]
    assert line["product_id"] == 1
    assert line["name_snapshot"] == "Widget"
    assert line["price_snapshot"] == "100.00"
    assert line["qty"] == 1


async def test_quick_buy_explicit_email_and_qty(
    client: AsyncClient, add_product
) -> None:
    """An explicit email is used verbatim; qty>1 multiplies the total."""
    await add_product(2, price=Decimal("50.00"), code="Q-2", qty=10, name="Gadget")

    resp = await client.post(
        _QUICK,
        json={
            "product_id": 2,
            "phone": "+37360000222",
            "email": "buyer@example.com",
            "qty": 3,
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == "buyer@example.com"
    assert body["delivery_name"] is None
    assert body["total"] == "150.00"
    assert body["items"][0]["qty"] == 3


async def test_quick_buy_decrements_stock(client: AsyncClient, add_product) -> None:
    """Stock is race-safely decremented: a second buy over the remainder 409s."""
    await add_product(3, price=Decimal("10.00"), code="Q-3", qty=2, name="Item")

    first = await client.post(_QUICK, json={"product_id": 3, "phone": "+3730"})
    assert first.status_code == 201, first.text

    # Remaining stock is 1 → a qty-2 quick buy must fail out-of-stock.
    second = await client.post(
        _QUICK, json={"product_id": 3, "phone": "+3730", "qty": 2}
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "out_of_stock"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
async def test_quick_buy_out_of_stock_409(client: AsyncClient, add_product) -> None:
    await add_product(4, price=Decimal("10.00"), code="Q-4", qty=1, name="Rare")

    resp = await client.post(_QUICK, json={"product_id": 4, "phone": "+3730", "qty": 5})

    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "out_of_stock"
    assert err["details"]["product_id"] == 4


async def test_quick_buy_unknown_product_404(client: AsyncClient) -> None:
    resp = await client.post(_QUICK, json={"product_id": 999, "phone": "+3730"})

    assert resp.status_code == 404, resp.text
    err = resp.json()["error"]
    assert err["code"] == "product_not_found"
    assert err["details"]["product_id"] == 999


async def test_quick_buy_inactive_product_404(client: AsyncClient, add_product) -> None:
    """An inactive product is treated as absent (404, not orderable)."""
    await add_product(
        5, price=Decimal("10.00"), code="Q-5", qty=5, name="Hidden", is_active=False
    )

    resp = await client.post(_QUICK, json={"product_id": 5, "phone": "+3730"})

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "product_not_found"


async def test_quick_buy_invalid_phone_422(client: AsyncClient, add_product) -> None:
    await add_product(6, price=Decimal("10.00"), code="Q-6", qty=5)

    resp = await client.post(_QUICK, json={"product_id": 6, "phone": "x"})

    assert resp.status_code == 422, resp.text


async def test_quick_buy_zero_qty_422(client: AsyncClient, add_product) -> None:
    await add_product(7, price=Decimal("10.00"), code="Q-7", qty=5)

    resp = await client.post(_QUICK, json={"product_id": 7, "phone": "+3730", "qty": 0})

    assert resp.status_code == 422, resp.text
