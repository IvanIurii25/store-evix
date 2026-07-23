"""API tests for the checkout + order endpoints (B6, §9).

Covers quote (pickup + courier), happy-path checkout (guest pickup + user
courier), COD field defaults, empty-cart / courier-no-address / out-of-stock
errors, and the guest-vs-user order-lookup authorization rules. The concurrency
(last-unit race) test is owned by the separate order test-agent.
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

_CHECKOUT = "/api/v1/checkout"
_QUOTE = "/api/v1/checkout/quote"
_CART_ITEMS = "/api/v1/cart/items"
_ORDERS = "/api/v1/orders"


async def _seed_line(
    client: AsyncClient,
    add_product,
    *,
    product_id: int,
    price: str,
    qty: int,
    stock: int = 100,
    code: str | None = None,
) -> None:
    """Insert a product then add a guest cart line (sets the cookie on ``client``)."""
    await add_product(
        product_id,
        price=Decimal(price),
        code=code or f"SKU-{product_id}",
        qty=stock,
    )
    resp = await client.post(_CART_ITEMS, json={"product_id": product_id, "qty": qty})
    assert resp.status_code == 201, resp.text


# --------------------------------------------------------------------------- #
# Quote
# --------------------------------------------------------------------------- #
async def test_quote_pickup_is_free(client: AsyncClient, add_product) -> None:
    await _seed_line(client, add_product, product_id=1, price="100.00", qty=2)

    resp = await client.post(_QUOTE, json={"delivery_type": "pickup"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["subtotal"] == "200.00"
    assert body["discount_total"] == "0"
    assert body["delivery_cost"] == "0"
    assert body["total"] == "200.00"
    assert body["delivery_type"] == "pickup"
    assert body["item_count"] == 2


async def test_quote_courier_adds_flat_rate(client: AsyncClient, add_product) -> None:
    # courier_rate default is 50 (config §2.6).
    await _seed_line(client, add_product, product_id=1, price="100.00", qty=1)

    resp = await client.post(
        _QUOTE,
        json={"delivery_type": "courier", "delivery_address_id": 5},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # courier_rate is Decimal("50") → serialized without forced scale.
    assert Decimal(body["delivery_cost"]) == Decimal("50")
    assert Decimal(body["total"]) == Decimal("150")


async def test_quote_courier_without_address_422(
    client: AsyncClient, add_product
) -> None:
    await _seed_line(client, add_product, product_id=1, price="100.00", qty=1)

    resp = await client.post(_QUOTE, json={"delivery_type": "courier"})

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "http_error"


async def test_quote_courier_inline_address(client: AsyncClient, add_product) -> None:
    """A guest inline courier address satisfies the requirement (no saved id)."""
    await _seed_line(client, add_product, product_id=1, price="100.00", qty=1)

    resp = await client.post(
        _QUOTE,
        json={
            "delivery_type": "courier",
            "delivery_address": {
                "full_name": "Ion Guest",
                "city": "Chișinău",
                "street": "str. Testului 1",
            },
        },
    )

    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["delivery_cost"]) == Decimal("50")


async def test_checkout_guest_courier_inline_snapshot(
    client: AsyncClient, add_product
) -> None:
    """Guest courier checkout snapshots the inline address onto the order."""
    await _seed_line(client, add_product, product_id=1, price="100.00", qty=1)

    resp = await client.post(
        _CHECKOUT,
        json={
            "email": "guest@example.com",
            "phone": "+37360000000",
            "delivery_type": "courier",
            "delivery_address": {
                "full_name": "Ion Guest",
                "city": "Chișinău",
                "street": "str. Testului 1",
                "zip": "MD-2001",
            },
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert Decimal(body["delivery_cost"]) == Decimal("50")
    assert body["delivery_address_id"] is None
    assert body["delivery_name"] == "Ion Guest"
    assert body["delivery_city"] == "Chișinău"
    assert body["delivery_street"] == "str. Testului 1"
    assert body["delivery_zip"] == "MD-2001"


async def test_quote_empty_cart_400(client: AsyncClient) -> None:
    resp = await client.post(_QUOTE, json={"delivery_type": "pickup"})

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["message"]


async def test_quote_does_not_create_order(client: AsyncClient, add_product) -> None:
    await _seed_line(client, add_product, product_id=1, price="10.00", qty=1)

    await client.post(_QUOTE, json={"delivery_type": "pickup"})
    orders = await client.get(f"{_ORDERS}/20260720-000001", params={"email": "x@x.io"})

    # No order was persisted by the quote.
    assert orders.status_code == 404


# --------------------------------------------------------------------------- #
# Checkout — happy path
# --------------------------------------------------------------------------- #
async def test_checkout_guest_pickup_cod(client: AsyncClient, add_product) -> None:
    await _seed_line(client, add_product, product_id=1, price="100.00", qty=2)

    resp = await client.post(
        _CHECKOUT,
        json={
            "email": "guest@example.com",
            "phone": "+37360000000",
            "delivery_type": "pickup",
        },
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "new"
    assert body["payment_status"] == "pending"
    assert body["payment_method"] == "cod"
    assert body["delivery_cost"] == "0"
    assert body["total"] == "200.00"
    assert body["email"] == "guest@example.com"
    assert body["delivery_address_id"] is None
    assert body["number"]
    assert len(body["items"]) == 1
    line = body["items"][0]
    assert line["product_id"] == 1
    assert line["name_snapshot"] == "Product"
    assert line["price_snapshot"] == "100.00"
    assert line["qty"] == 2


async def test_checkout_number_format(client: AsyncClient, add_product) -> None:
    await _seed_line(client, add_product, product_id=1, price="10.00", qty=1)

    resp = await client.post(
        _CHECKOUT,
        json={
            "email": "g@example.com",
            "phone": "+3730",
            "delivery_type": "pickup",
        },
    )

    number = resp.json()["number"]
    date_part, _, seq_part = number.partition("-")
    assert len(date_part) == 8 and date_part.isdigit()
    assert len(seq_part) == 6 and seq_part.isdigit()


async def test_checkout_decrements_stock(client: AsyncClient, add_product) -> None:
    await _seed_line(client, add_product, product_id=1, price="10.00", qty=3, stock=5)

    resp = await client.post(
        _CHECKOUT,
        json={
            "email": "g@example.com",
            "phone": "+3730",
            "delivery_type": "pickup",
        },
    )
    assert resp.status_code == 201

    from app.models.catalog import Product  # local import: assertion-only

    # Re-read stock via a fresh service session would need DB; use the app's
    # override session indirectly by re-checking via a second checkout attempt.
    # Simpler: the remaining stock is 2 → a 3-qty second order must 409.
    await client.post(_CART_ITEMS, json={"product_id": 1, "qty": 3})
    second = await client.post(
        _CHECKOUT,
        json={
            "email": "g@example.com",
            "phone": "+3730",
            "delivery_type": "pickup",
        },
    )
    assert second.status_code == 409, second.text
    assert Product  # keep import referenced


async def test_checkout_converts_cart(client: AsyncClient, add_product) -> None:
    await _seed_line(client, add_product, product_id=1, price="10.00", qty=1)
    await client.post(
        _CHECKOUT,
        json={"email": "g@x.io", "phone": "+3730", "delivery_type": "pickup"},
    )

    # Cart was converted → GET /cart is now empty (a fresh draft has no lines).
    cart = await client.get("/api/v1/cart")
    assert cart.status_code == 200
    assert cart.json()["item_count"] == 0


# --------------------------------------------------------------------------- #
# Checkout — errors
# --------------------------------------------------------------------------- #
async def test_checkout_empty_cart_400(client: AsyncClient) -> None:
    resp = await client.post(
        _CHECKOUT,
        json={"email": "g@x.io", "phone": "+3730", "delivery_type": "pickup"},
    )
    assert resp.status_code == 400, resp.text


async def test_checkout_out_of_stock_409(client: AsyncClient, add_product) -> None:
    # Request more than in stock: cart qty 5, stock 2.
    await _seed_line(client, add_product, product_id=1, price="10.00", qty=5, stock=2)

    resp = await client.post(
        _CHECKOUT,
        json={"email": "g@x.io", "phone": "+3730", "delivery_type": "pickup"},
    )

    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "out_of_stock"
    assert err["details"]["product_id"] == 1


async def test_checkout_courier_without_address_422(
    client: AsyncClient, add_product
) -> None:
    await _seed_line(client, add_product, product_id=1, price="10.00", qty=1)

    resp = await client.post(
        _CHECKOUT,
        json={"email": "g@x.io", "phone": "+3730", "delivery_type": "courier"},
    )
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------- #
# Checkout — courier for a user + order lookup
# --------------------------------------------------------------------------- #
async def test_checkout_user_courier_and_lookup(
    user_client: AsyncClient, add_product, add_address
) -> None:
    await add_product(1, price=Decimal("100.00"), code="U-1", qty=10)
    await add_address(42)
    add_resp = await user_client.post(_CART_ITEMS, json={"product_id": 1, "qty": 1})
    assert add_resp.status_code == 201

    resp = await user_client.post(
        _CHECKOUT,
        json={
            "email": "checkout-user@example.com",
            "phone": "+3730",
            "delivery_type": "courier",
            "delivery_address_id": 42,
        },
    )
    assert resp.status_code == 201, resp.text
    number = resp.json()["number"]
    assert Decimal(resp.json()["delivery_cost"]) == Decimal("50")
    assert resp.json()["delivery_address_id"] == 42

    # GET /orders lists the user's order.
    listing = await user_client.get(_ORDERS)
    assert listing.status_code == 200
    numbers = [o["number"] for o in listing.json()]
    assert number in numbers

    # GET /orders/{number} by JWT owner (no email needed).
    detail = await user_client.get(f"{_ORDERS}/{number}")
    assert detail.status_code == 200
    assert detail.json()["number"] == number


async def test_list_orders_requires_auth(client: AsyncClient) -> None:
    resp = await client.get(_ORDERS)
    assert resp.status_code == 401, resp.text


async def test_guest_order_lookup_by_email(client: AsyncClient, add_product) -> None:
    await _seed_line(client, add_product, product_id=1, price="10.00", qty=1)
    created = await client.post(
        _CHECKOUT,
        json={
            "email": "guest@shop.io",
            "phone": "+3730",
            "delivery_type": "pickup",
        },
    )
    number = created.json()["number"]

    ok = await client.get(f"{_ORDERS}/{number}", params={"email": "guest@shop.io"})
    assert ok.status_code == 200
    assert ok.json()["number"] == number

    # Case-insensitive email match.
    ci = await client.get(f"{_ORDERS}/{number}", params={"email": "GUEST@SHOP.IO"})
    assert ci.status_code == 200

    # Wrong email → 404 (existence not leaked).
    wrong = await client.get(f"{_ORDERS}/{number}", params={"email": "other@x.io"})
    assert wrong.status_code == 404

    # No email as a guest → 404.
    none = await client.get(f"{_ORDERS}/{number}")
    assert none.status_code == 404
