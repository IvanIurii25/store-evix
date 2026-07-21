"""Data-access layer for the business dashboard aggregates (§6.3).

Pure SQL/ORM access — no business rules, no HTTP. Aggregates orders and order
lines for the back-office overview: revenue + order counts over a window, the
status distribution, low-stock products, and best sellers. Revenue excludes
canceled orders (they carry no revenue); counts include every order in range.

All range queries take a half-open ``[start, end)`` datetime window.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Product
from app.models.order import Order, OrderItem


@dataclass(frozen=True)
class RevenueTotals:
    """Revenue + order counts for a window (§6.3)."""

    revenue: Decimal
    orders_count: int
    paid_orders_count: int


@dataclass(frozen=True)
class RevenuePoint:
    """One day bucket of revenue + orders (§6.3)."""

    day: datetime
    revenue: Decimal
    orders_count: int


@dataclass(frozen=True)
class TopProduct:
    """A best-selling product line (§6.3)."""

    product_id: int | None
    name: str
    qty_sold: int
    revenue: Decimal


class AdminDashboardRepository:
    """Read aggregates for the business dashboard, bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used by every query method.

        Args:
            session: Active async session (request- or test-scoped).
        """
        self.session = session

    async def revenue_totals(self, start: datetime, end: datetime) -> RevenueTotals:
        """Return revenue (excl. canceled) + total / non-canceled order counts."""
        in_range = (Order.created_at >= start, Order.created_at < end)
        revenue = (
            await self.session.scalar(
                select(func.coalesce(func.sum(Order.total), 0)).where(
                    *in_range, Order.status != "canceled"
                )
            )
        ) or Decimal("0")
        orders_count = (
            await self.session.scalar(
                select(func.count()).select_from(Order).where(*in_range)
            )
        ) or 0
        paid_orders = (
            await self.session.scalar(
                select(func.count())
                .select_from(Order)
                .where(*in_range, Order.status != "canceled")
            )
        ) or 0
        return RevenueTotals(
            revenue=Decimal(revenue),
            orders_count=int(orders_count),
            paid_orders_count=int(paid_orders),
        )

    async def status_distribution(
        self,
        start: datetime,
        end: datetime,
    ) -> list[tuple[str, int]]:
        """Return ``(status, count)`` for orders in the window."""
        stmt = (
            select(Order.status, func.count().label("n"))
            .where(Order.created_at >= start, Order.created_at < end)
            .group_by(Order.status)
            .order_by(func.count().desc())
        )
        return [(row.status, int(row.n)) for row in (await self.session.execute(stmt))]

    async def revenue_series(
        self,
        start: datetime,
        end: datetime,
    ) -> list[RevenuePoint]:
        """Return per-day revenue (excl. canceled) + order count, ascending."""
        day = func.date_trunc("day", Order.created_at).label("day")
        stmt = (
            select(
                day,
                func.coalesce(
                    func.sum(Order.total).filter(Order.status != "canceled"), 0
                ).label("revenue"),
                func.count().label("orders_count"),
            )
            .where(Order.created_at >= start, Order.created_at < end)
            .group_by(day)
            .order_by(day)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            RevenuePoint(
                day=row.day,
                revenue=Decimal(row.revenue),
                orders_count=int(row.orders_count),
            )
            for row in rows
        ]

    async def low_stock(self, threshold: int, limit: int) -> list[Product]:
        """Return products at/below ``threshold`` stock, lowest first."""
        stmt = (
            select(Product)
            .where(Product.qty <= threshold)
            .order_by(Product.qty, Product.id)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def low_stock_count(self, threshold: int) -> int:
        """Return how many products are at/below ``threshold`` stock."""
        stmt = select(func.count()).select_from(Product).where(Product.qty <= threshold)
        return int((await self.session.scalar(stmt)) or 0)

    async def top_products(
        self,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[TopProduct]:
        """Return best sellers by units sold in the window (excl. canceled).

        Uses the order-line snapshots joined to their order, so it survives
        product deletion (``product_id`` may be null; the snapshot name stays).
        """
        stmt = (
            select(
                OrderItem.product_id,
                func.max(OrderItem.name_snapshot).label("name"),
                func.sum(OrderItem.qty).label("qty_sold"),
                func.sum(OrderItem.price_snapshot * OrderItem.qty).label("revenue"),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.created_at >= start,
                Order.created_at < end,
                Order.status != "canceled",
            )
            .group_by(OrderItem.product_id)
            .order_by(func.sum(OrderItem.qty).desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            TopProduct(
                product_id=row.product_id,
                name=row.name,
                qty_sold=int(row.qty_sold),
                revenue=Decimal(row.revenue),
            )
            for row in rows
        ]
