"""Concurrency: two admins race to cancel the same order (§8, H1 regression).

Contract: the order transition loads its row ``FOR UPDATE`` (see
``OrderRepository.get_order_by_number(..., for_update=True)``), so two concurrent
cancels serialize. Exactly one wins (``new -> canceled``, stock returned once);
the other blocks, re-reads the now-``canceled`` row, and is rejected by the state
machine (``IllegalTransitionError``). Without the lock both would pass the
in-memory validate, both commit, and the post-commit stock return runs twice —
over-crediting inventory and writing two audit rows.

Like the last-unit race, this opts out of SAVEPOINT isolation: two competing real
commits cannot happen inside a single rolled-back transaction. Two independent
engines each run their own committing session; ``real_engine`` truncates the
touched tables afterwards so nothing leaks into the rest of the suite.
"""

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.catalog import Category, Product
from app.models.order import Order, OrderItem, OrderStatusHistory
from app.services.admin_order_service import AdminOrderService
from app.services.order_service import IllegalTransitionError

pytestmark = pytest.mark.asyncio

_ORDER_NUMBER = "2026-000001"
_START_QTY = 5
_LINE_QTY = 3


async def _seed_cancelable_order(factory: async_sessionmaker) -> None:
    """Commit one ``new`` order with a single line against an in-stock product."""
    async with factory() as session:
        session.add(Category(id=1, parent_id=None, path=[1], depth=0, is_active=True))
        session.add(
            Product(
                id=1,
                category_id=1,
                code="CANCELME",
                price=Decimal("300.00"),
                qty=_START_QTY,
                is_active=True,
            )
        )
        session.add(
            Order(
                id=1,
                number=_ORDER_NUMBER,
                email="race@example.com",
                phone="+37360000010",
                status="new",
                payment_status="pending",
                subtotal=Decimal("900.00"),
                total=Decimal("900.00"),
                delivery_type="pickup",
            )
        )
        await session.flush()
        session.add(
            OrderItem(
                order_id=1,
                product_id=1,
                name_snapshot="CancelMe",
                price_snapshot=Decimal("300.00"),
                qty=_LINE_QTY,
            )
        )
        await session.commit()


async def _cancel(factory: async_sessionmaker) -> None:
    """Cancel the seeded order through the admin service on its own session."""
    async with factory() as session:
        service = AdminOrderService(session)
        await service.transition(
            _ORDER_NUMBER, to_status="canceled", changed_by="admin"
        )


async def test_two_concurrent_cancels_return_stock_once(real_engine) -> None:
    """One concurrent cancel wins; the other is rejected and stock returns once."""
    _, factory_seed = real_engine()
    await _seed_cancelable_order(factory_seed)

    _, factory_a = real_engine()
    _, factory_b = real_engine()

    results = await asyncio.gather(
        _cancel(factory_a),
        _cancel(factory_b),
        return_exceptions=True,
    )

    # Exactly one cancel succeeded; the loser was rejected by the state machine.
    rejections = [r for r in results if isinstance(r, IllegalTransitionError)]
    successes = [r for r in results if r is None]
    assert len(successes) == 1, f"expected one winner, got results={results}"
    assert len(rejections) == 1, f"expected one rejection, got results={results}"

    async with factory_seed() as session:
        # Stock returned exactly once: start + a single line's worth.
        product = (
            await session.execute(select(Product).where(Product.id == 1))
        ).scalar_one()
        assert product.qty == _START_QTY + _LINE_QTY

        # Exactly one audit row records the cancel.
        canceled_rows = (
            await session.execute(
                select(func.count())
                .select_from(OrderStatusHistory)
                .where(OrderStatusHistory.to_status == "canceled")
            )
        ).scalar_one()
        assert canceled_rows == 1

        order = (
            await session.execute(select(Order).where(Order.id == 1))
        ).scalar_one()
        assert order.status == "canceled"
