"""End-to-end card-payment (maib) flow tests over the public HTTP contract.

Each test seeds a product, fills a guest cart through the cart router, then drives
``POST /checkout`` (``payment_method=card``) and the ``POST /payments/maib/callback``
webhook — asserting the order settles / cancels correctly, stock is returned on a
failed payment, the callback is idempotent, and the disabled path is rejected.
COD is covered by a dedicated regression test so its behaviour is proven intact.
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Product
from app.models.order import Order
from app.models.payment import Payment
from tests.payment.conftest import StubMaibClient

pytestmark = pytest.mark.asyncio


async def _seed_cart(client: AsyncClient, product_id: int, qty: int = 1) -> None:
    """Add ``qty`` of a product to the guest cart (sets the session cookie)."""
    resp = await client.post(
        "/api/v1/cart/items", json={"product_id": product_id, "qty": qty}
    )
    assert resp.status_code == 201, resp.text


async def _checkout_card(client: AsyncClient) -> dict:
    """Run a card checkout and return the order JSON (with ``pay_url``)."""
    resp = await client.post(
        "/api/v1/checkout",
        json={
            "email": "card@example.com",
            "phone": "+37360000001",
            "delivery_type": "pickup",
            "payment_method": "card",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _callback_body(stub: StubMaibClient, *, order_number: str, status: str) -> dict:
    """Build a signed callback payload for the stub's pay id."""
    result = {
        "payId": stub.last_pay_id,
        "orderId": order_number,
        "status": status,
        "statusCode": "000",
        "amount": 100.0,
        "currency": "MDL",
    }
    return {"result": result, "signature": stub.sign(result)}


async def test_card_checkout_returns_pay_url(
    client: AsyncClient,
    add_product,
    stub_maib: StubMaibClient,
) -> None:
    """A card checkout creates a pending order + payment and returns the payUrl."""
    await add_product(1, price=Decimal("100.00"), code="CARD1")
    await _seed_cart(client, 1)
    order = await _checkout_card(client)

    assert order["payment_method"] == "card"
    assert order["payment_status"] == "pending"
    assert order["pay_url"] == f"https://maib.test/checkout/{stub_maib.last_pay_id}"


async def test_callback_ok_marks_order_paid(
    client: AsyncClient,
    add_product,
    stub_maib: StubMaibClient,
    db_session: AsyncSession,
) -> None:
    """A signed ``OK`` callback settles the order to ``paid``."""
    await add_product(1, price=Decimal("100.00"), code="CARD2")
    await _seed_cart(client, 1)
    order = await _checkout_card(client)

    resp = await client.post(
        "/api/v1/payments/maib/callback",
        json=_callback_body(stub_maib, order_number=order["number"], status="OK"),
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": True}

    row = (
        await db_session.execute(
            select(Order).where(Order.number == order["number"])
        )
    ).scalar_one()
    assert row.payment_status == "paid"
    payment = (
        await db_session.execute(
            select(Payment).where(Payment.pay_id == stub_maib.last_pay_id)
        )
    ).scalar_one()
    assert payment.status == "ok"


async def test_callback_not_ok_cancels_and_returns_stock(
    client: AsyncClient,
    add_product,
    stub_maib: StubMaibClient,
    db_session: AsyncSession,
) -> None:
    """A non-OK callback cancels the order and returns the reserved stock."""
    await add_product(1, price=Decimal("100.00"), code="CARD3", qty=5)
    await _seed_cart(client, 1, qty=2)
    order = await _checkout_card(client)

    # Stock was decremented at checkout (5 - 2 = 3).
    product = (
        await db_session.execute(select(Product).where(Product.id == 1))
    ).scalar_one()
    assert product.qty == 3

    resp = await client.post(
        "/api/v1/payments/maib/callback",
        json=_callback_body(
            stub_maib, order_number=order["number"], status="FAILED"
        ),
    )
    assert resp.status_code == 200

    await db_session.refresh(product)
    order_row = (
        await db_session.execute(
            select(Order).where(Order.number == order["number"])
        )
    ).scalar_one()
    assert order_row.status == "canceled"
    assert product.qty == 5, "stock must be returned on a failed card payment"


async def test_callback_is_idempotent(
    client: AsyncClient,
    add_product,
    stub_maib: StubMaibClient,
    db_session: AsyncSession,
) -> None:
    """A repeated OK callback is a no-op (no double-settlement / extra history)."""
    await add_product(1, price=Decimal("100.00"), code="CARD4")
    await _seed_cart(client, 1)
    order = await _checkout_card(client)
    body = _callback_body(stub_maib, order_number=order["number"], status="OK")

    first = await client.post("/api/v1/payments/maib/callback", json=body)
    second = await client.post("/api/v1/payments/maib/callback", json=body)
    assert first.json() == {"accepted": True}
    assert second.json() == {"accepted": True}

    row = (
        await db_session.execute(
            select(Order).where(Order.number == order["number"])
        )
    ).scalar_one()
    assert row.payment_status == "paid"


async def test_callback_bad_signature_rejected(
    client: AsyncClient,
    add_product,
    stub_maib: StubMaibClient,
    db_session: AsyncSession,
) -> None:
    """An invalid signature is rejected and the order is left untouched."""
    await add_product(1, price=Decimal("100.00"), code="CARD5")
    await _seed_cart(client, 1)
    order = await _checkout_card(client)

    body = _callback_body(stub_maib, order_number=order["number"], status="OK")
    body["signature"] = "tampered-signature"
    resp = await client.post("/api/v1/payments/maib/callback", json=body)
    assert resp.status_code == 200
    assert resp.json() == {"accepted": False}

    row = (
        await db_session.execute(
            select(Order).where(Order.number == order["number"])
        )
    ).scalar_one()
    assert row.payment_status == "pending", "bad signature must not settle the order"


async def test_card_rejected_when_disabled(
    client: AsyncClient,
    add_product,
    monkeypatch,
) -> None:
    """With card payment disabled, a card checkout is rejected 400."""
    monkeypatch.setattr("app.core.config.settings.maib_signature_key", "")
    await add_product(1, price=Decimal("100.00"), code="CARD6")
    await _seed_cart(client, 1)

    resp = await client.post(
        "/api/v1/checkout",
        json={
            "email": "card@example.com",
            "phone": "+37360000001",
            "delivery_type": "pickup",
            "payment_method": "card",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "card_payment_disabled"


async def test_cod_checkout_unchanged(
    client: AsyncClient,
    add_product,
    db_session: AsyncSession,
) -> None:
    """A COD checkout still works with no pay_url and no payment row (regression)."""
    await add_product(1, price=Decimal("100.00"), code="COD1")
    await _seed_cart(client, 1)

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
    assert body["pay_url"] is None
    assert body["payment_status"] == "pending"

    payments = (
        await db_session.execute(
            select(Payment).join(Order, Payment.order_id == Order.id).where(
                Order.number == body["number"]
            )
        )
    ).scalars().all()
    assert payments == [], "COD must create no payment row"
