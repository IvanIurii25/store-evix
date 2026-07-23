"""State-machine tests for :class:`OrderService.transition` (B6, §8).

Covers the legal / illegal transition matrix for both axes (fulfilment
``status`` and COD ``payment_status``), that each accepted move writes an
``order_status_history`` row, and that an illegal move mutates nothing.
"""

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.repositories.order_repo import OrderRepository
from app.services.order_service import (
    IllegalTransitionError,
    OrderService,
)

pytestmark = pytest.mark.asyncio

# Enumerated legal/illegal moves (§8). ``(current, target, legal)``.
_STATUS_MATRIX: list[tuple[str, str, bool]] = [
    ("new", "confirmed", True),
    ("new", "canceled", True),
    ("new", "done", False),
    ("new", "new", False),
    ("confirmed", "done", True),
    ("confirmed", "canceled", True),
    ("confirmed", "new", False),
    ("done", "confirmed", False),
    ("done", "canceled", False),
    ("canceled", "confirmed", False),
    ("canceled", "done", False),
]

_PAYMENT_MATRIX: list[tuple[str, str, bool]] = [
    ("pending", "paid", True),
    ("pending", "refunded", False),
    ("paid", "refunded", True),
    ("paid", "pending", False),
    ("refunded", "paid", False),
    ("refunded", "pending", False),
]

_SEQ = iter(range(1, 10_000))


async def _make_order(
    session: AsyncSession,
    *,
    status: str = "new",
    payment_status: str = "pending",
) -> Order:
    """Persist and return a minimal order in the requested state."""
    n = next(_SEQ)
    order = Order(
        number=f"SM-{n:06d}",
        user_id=None,
        email="sm@example.com",
        phone="+3730",
        status=status,
        payment_status=payment_status,
        subtotal=Decimal("10.00"),
        discount_total=Decimal("0"),
        delivery_cost=Decimal("0"),
        total=Decimal("10.00"),
        delivery_type="pickup",
        payment_method="cod",
    )
    session.add(order)
    await session.flush()
    return order


@pytest.mark.parametrize(("current", "target", "legal"), _STATUS_MATRIX)
async def test_status_transition_matrix(
    db_session: AsyncSession, current: str, target: str, legal: bool
) -> None:
    order = await _make_order(db_session, status=current)
    service = OrderService(db_session)
    repo = OrderRepository(db_session)

    if legal:
        await service.transition(order, to_status=target, changed_by="admin")
        assert order.status == target
        assert await repo.count_status_history(order.id) == 1
    else:
        with pytest.raises(IllegalTransitionError):
            await service.transition(order, to_status=target)
        assert order.status == current
        assert await repo.count_status_history(order.id) == 0


@pytest.mark.parametrize(("current", "target", "legal"), _PAYMENT_MATRIX)
async def test_payment_transition_matrix(
    db_session: AsyncSession, current: str, target: str, legal: bool
) -> None:
    order = await _make_order(db_session, payment_status=current)
    service = OrderService(db_session)
    repo = OrderRepository(db_session)

    if legal:
        await service.transition(order, to_payment_status=target, changed_by="admin")
        assert order.payment_status == target
        assert await repo.count_status_history(order.id) == 1
    else:
        with pytest.raises(IllegalTransitionError):
            await service.transition(order, to_payment_status=target)
        assert order.payment_status == current
        assert await repo.count_status_history(order.id) == 0


async def test_history_records_from_and_to(db_session: AsyncSession) -> None:
    order = await _make_order(db_session, status="new")
    service = OrderService(db_session)

    await service.transition(order, to_status="confirmed", changed_by="admin")

    from sqlalchemy import select

    from app.models.order import OrderStatusHistory

    rows = (
        (
            await db_session.execute(
                select(OrderStatusHistory).where(
                    OrderStatusHistory.order_id == order.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].from_status == "new"
    assert rows[0].to_status == "confirmed"
    assert rows[0].changed_by == "admin"


async def test_both_axes_in_one_call(db_session: AsyncSession) -> None:
    order = await _make_order(db_session, status="new", payment_status="pending")
    service = OrderService(db_session)
    repo = OrderRepository(db_session)

    await service.transition(
        order, to_status="confirmed", to_payment_status="paid", changed_by="admin"
    )

    assert order.status == "confirmed"
    assert order.payment_status == "paid"
    # One history row per moved axis.
    assert await repo.count_status_history(order.id) == 2


async def test_illegal_axis_blocks_both(db_session: AsyncSession) -> None:
    # Legal status move + illegal payment move → whole call rejected, no mutation.
    order = await _make_order(db_session, status="new", payment_status="pending")
    service = OrderService(db_session)
    repo = OrderRepository(db_session)

    with pytest.raises(IllegalTransitionError):
        await service.transition(
            order, to_status="confirmed", to_payment_status="refunded"
        )

    assert order.status == "new"
    assert order.payment_status == "pending"
    assert await repo.count_status_history(order.id) == 0


async def test_cancel_side_effect_runs_after_commit(
    db_session: AsyncSession, caplog
) -> None:
    order = await _make_order(db_session, status="confirmed")
    service = OrderService(db_session)

    with caplog.at_level("INFO"):
        await service.transition(order, to_status="canceled", changed_by="admin")

    assert order.status == "canceled"
    assert any("stock-return" in rec.message for rec in caplog.records)
