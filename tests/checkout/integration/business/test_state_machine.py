"""State-machine tests for :class:`OrderService.transition` (B6, §8).

Covers the legal / illegal transition matrix for both axes (fulfilment
``status`` and COD ``payment_status``), that each accepted move writes an
``order_status_history`` row, and that an illegal move mutates nothing.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Category, Product, ProductTranslation
from app.models.order import Order, OrderItem
from app.repositories.order_repo import OrderRepository
from app.services.order_service import (
    IllegalTransitionError,
    OrderService,
)

pytestmark = pytest.mark.asyncio

# Category id reused for all products created by the stock-return helpers below
# (``product.category_id`` FK). Created lazily and idempotently.
_STOCK_CATEGORY_ID: int = 9001

# Sequence for unique product ids/codes in the stock-return tests.
_PROD_SEQ = iter(range(9100, 20_000))

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


async def _make_product(
    session: AsyncSession,
    *,
    qty: int,
) -> Product:
    """Persist an active product (+ its category) with the given stock ``qty``."""
    if await session.get(Category, _STOCK_CATEGORY_ID) is None:
        session.add(
            Category(
                id=_STOCK_CATEGORY_ID,
                parent_id=None,
                path=[_STOCK_CATEGORY_ID],
                depth=0,
                is_active=True,
            )
        )
        await session.flush()
    prod_n = next(_PROD_SEQ)
    product = Product(
        id=prod_n,
        category_id=_STOCK_CATEGORY_ID,
        code=f"SR-{prod_n}",
        price=Decimal("10.00"),
        qty=qty,
        is_active=True,
    )
    session.add(product)
    session.add(
        ProductTranslation(
            product_id=prod_n,
            lang="ro",
            name=f"prod-{prod_n}",
            slug=f"slug-{prod_n}",
        )
    )
    await session.flush()
    return product


async def _add_line(
    session: AsyncSession,
    order: Order,
    product: Product,
    *,
    qty: int,
) -> None:
    """Attach one snapshot line referencing ``product`` to ``order``."""
    session.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            name_snapshot=product.code,
            price_snapshot=product.price,
            qty=qty,
        )
    )
    await session.flush()


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


async def test_cancel_returns_stock_after_commit(
    db_session: AsyncSession,
) -> None:
    # A confirmed order whose line already decremented stock at checkout: the
    # product started at 10, checkout took 3 → 7 in stock; cancel must add the 3
    # back (7 → 10). The side effect commits its own increment after the status
    # commit, so re-reading the product reflects the return.
    product = await _make_product(db_session, qty=7)
    order = await _make_order(db_session, status="confirmed")
    await _add_line(db_session, order, product, qty=3)
    service = OrderService(db_session)

    # No active restock subscriber (qty stays > 0), so the notify task is patched
    # only to keep this test independent of the enqueue path.
    with patch("app.tasks.restock.send_restock_notifications"):
        await service.transition(order, to_status="canceled", changed_by="admin")

    assert order.status == "canceled"
    await db_session.refresh(product)
    assert product.qty == 10, "canceled line's stock must be returned"


async def test_cancel_crossing_zero_enqueues_restock(
    db_session: AsyncSession,
) -> None:
    # Product fully sold out at checkout (qty 0). Cancel returns its unit(s),
    # crossing 0 → >0, which must enqueue the restock notification exactly once
    # for that product. The Celery task is patched (guide §11 mock policy) so no
    # real broker/worker is exercised.
    product = await _make_product(db_session, qty=0)
    order = await _make_order(db_session, status="new")
    await _add_line(db_session, order, product, qty=4)
    service = OrderService(db_session)

    with patch("app.tasks.restock.send_restock_notifications") as task:
        await service.transition(order, to_status="canceled", changed_by="admin")

    await db_session.refresh(product)
    assert product.qty == 4, "returned stock crosses back into stock"
    task.delay.assert_called_once_with(product.id)


async def test_cancel_no_restock_when_stock_not_from_zero(
    db_session: AsyncSession,
) -> None:
    # Product was still in stock (qty 5) when the order is canceled: returning
    # the line does NOT cross 0 → >0, so no restock notification is enqueued.
    product = await _make_product(db_session, qty=5)
    order = await _make_order(db_session, status="new")
    await _add_line(db_session, order, product, qty=2)
    service = OrderService(db_session)

    with patch("app.tasks.restock.send_restock_notifications") as task:
        await service.transition(order, to_status="canceled", changed_by="admin")

    await db_session.refresh(product)
    assert product.qty == 7
    task.delay.assert_not_called()
