"""Full-cycle business test: checkout decrements stock, cancel returns it (§8).

Exercises the real end-to-end path the bug regressed on: a guest checks out
through the API (which atomically decrements product stock), then the order is
canceled through :class:`OrderService.transition`, whose post-commit side effect
must credit every line's quantity back to inventory. The restock Celery task is
patched (guide §11 mock policy) so no broker/worker is touched.

Cancel is terminal (``canceled`` has no outgoing transitions), so the stock
return can only fire once per order — there is no separate idempotency test.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Product
from app.models.order import Order
from app.services.order_service import OrderService

pytestmark = pytest.mark.asyncio

_CHECKOUT = "/api/v1/checkout"
_CART_ITEMS = "/api/v1/cart/items"

_PRODUCT_ID: int = 8801
_START_STOCK: int = 10
_ORDER_QTY: int = 4


async def test_checkout_then_cancel_restores_stock(
    client: AsyncClient,
    db_session: AsyncSession,
    add_product,
) -> None:
    # Arrange — an in-stock product and a guest cart line.
    await add_product(
        _PRODUCT_ID,
        price=Decimal("100.00"),
        code="CANCEL-CYCLE",
        qty=_START_STOCK,
    )
    resp = await client.post(
        _CART_ITEMS, json={"product_id": _PRODUCT_ID, "qty": _ORDER_QTY}
    )
    assert resp.status_code == 201, resp.text

    # Act 1 — checkout decrements stock (10 → 6).
    resp = await client.post(
        _CHECKOUT,
        json={
            "delivery_type": "pickup",
            "email": "guest@example.com",
            "phone": "+37360000000",
        },
    )
    assert resp.status_code == 201, resp.text
    number = resp.json()["number"]

    product = await db_session.get(Product, _PRODUCT_ID)
    await db_session.refresh(product)
    assert product.qty == _START_STOCK - _ORDER_QTY, "checkout must decrement stock"

    # Act 2 — cancel the freshly created order via the state machine.
    order = (
        await db_session.execute(select(Order).where(Order.number == number))
    ).scalar_one()
    with patch("app.tasks.restock.send_restock_notifications"):
        await OrderService(db_session).transition(
            order, to_status="canceled", changed_by="admin"
        )

    # Assert — the canceled order's stock is fully returned (6 → 10).
    await db_session.refresh(product)
    assert product.qty == _START_STOCK, "cancel must return the decremented stock"
