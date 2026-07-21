"""Data-access layer for the customers back office (admin §6.2).

Pure SQL/ORM access — no business rules, no HTTP. Customers are :class:`AppUser`
rows; their order stats (count / lifetime spend / last order) come from the
``order`` table keyed by ``user_id`` (guest orders have a null ``user_id`` and
are naturally excluded). The stats query is *batched* — one grouped SELECT for a
whole page of customers — so the list never issues a per-customer N+1.

Lifetime spend excludes canceled orders (they carry no revenue); the count and
last-order timestamp include every order the customer placed.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.models.user import Address, AppUser


@dataclass(frozen=True)
class CustomerStats:
    """Aggregated order stats for one customer (§6.2)."""

    orders_count: int
    total_spent: Decimal
    last_order_at: datetime | None


class AdminCustomerRepository:
    """Read queries for the customers list + detail, bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used by every query method.

        Args:
            session: Active async session (request- or test-scoped).
        """
        self.session = session

    @staticmethod
    def _apply_search(stmt: Select, query: str | None) -> Select:
        """Attach the optional free-text search over email / phone.

        Args:
            stmt: The base ``SELECT`` (over ``AppUser`` or a count).
            query: Free-text term, or ``None`` for no filter.

        Returns:
            Select: The statement with the search predicate applied.
        """
        if query:
            pattern = f"%{query}%"
            stmt = stmt.where(
                or_(
                    AppUser.email.ilike(pattern),
                    AppUser.phone.ilike(pattern),
                )
            )
        return stmt

    async def list_customers(
        self,
        *,
        query: str | None,
        offset: int,
        limit: int,
    ) -> list[AppUser]:
        """Return one page of customers (newest first) matching the search.

        Args:
            query: Free-text search over email / phone, or ``None``.
            offset: Rows to skip (``(page - 1) * page_size``).
            limit: Page size.

        Returns:
            list[AppUser]: The page of customers, newest first.
        """
        stmt = self._apply_search(select(AppUser), query)
        stmt = stmt.order_by(AppUser.id.desc()).offset(offset).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_customers(self, *, query: str | None) -> int:
        """Return the total number of customers matching the search."""
        base = select(func.count()).select_from(AppUser)
        stmt = self._apply_search(base, query)
        return int((await self.session.scalar(stmt)) or 0)

    async def stats_for(self, user_ids: list[int]) -> dict[int, CustomerStats]:
        """Return order stats per user id in one batched grouped query.

        Args:
            user_ids: The customer ids to aggregate (a page's worth).

        Returns:
            dict[int, CustomerStats]: ``user_id -> stats``. Ids with no orders
                are absent (the service fills a zero default).
        """
        if not user_ids:
            return {}
        spent = func.coalesce(
            func.sum(Order.total).filter(Order.status != "canceled"),
            0,
        )
        stmt = (
            select(
                Order.user_id,
                func.count().label("orders_count"),
                spent.label("total_spent"),
                func.max(Order.created_at).label("last_order_at"),
            )
            .where(Order.user_id.in_(user_ids))
            .group_by(Order.user_id)
        )
        rows = (await self.session.execute(stmt)).all()
        return {
            row.user_id: CustomerStats(
                orders_count=int(row.orders_count),
                total_spent=Decimal(row.total_spent),
                last_order_at=row.last_order_at,
            )
            for row in rows
        }

    async def get_customer(self, user_id: int) -> AppUser | None:
        """Fetch one customer by id, or ``None`` if absent."""
        return await self.session.get(AppUser, user_id)

    async def list_addresses(self, user_id: int) -> list[Address]:
        """Return all addresses owned by ``user_id`` ordered by id."""
        stmt = select(Address).where(Address.user_id == user_id).order_by(Address.id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_orders(self, user_id: int) -> list[Order]:
        """Return all of a customer's orders, newest first."""
        stmt = (
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc(), Order.id.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())
