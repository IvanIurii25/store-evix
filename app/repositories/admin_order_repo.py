"""Async data-access layer for the admin orders back office (stage B7, §10).

Only SQL / query construction lives here — no business rules (those belong to
:class:`~app.services.admin_order_service.AdminOrderService`). All statements are
async SQLAlchemy 2.0 bound to one session.

The list query supports:

* an optional exact ``status`` filter (fulfilment axis, §8);
* an optional free-text ``q`` matched case-insensitively against ``number`` /
  ``email`` / ``phone`` (the fields a back-office operator searches by);
* offset pagination returning both the page slice and the total count so the
  ``{data, total, page, page_size}`` envelope (§4) can be assembled.

Order detail and line hydration reuse
:class:`~app.repositories.order_repo.OrderRepository` (get-by-number,
``list_items_for_orders``) — this repo does not duplicate those reads.
"""

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order


class AdminOrderRepository:
    """Read queries for the admin orders list, bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used by every query method.

        Args:
            session: Active async session (request-scoped or test-scoped).
        """
        self.session = session

    @staticmethod
    def _apply_filters(
        stmt: Select,
        status: str | None,
        query: str | None,
    ) -> Select:
        """Attach the optional ``status`` filter and ``q`` search to a statement.

        Args:
            stmt: The base ``SELECT`` (over ``Order`` or a count).
            status: Exact fulfilment status to filter by, or ``None``.
            query: Free-text search over ``number`` / ``email`` / ``phone``, or
                ``None``.

        Returns:
            Select: The statement with the requested predicates applied.
        """
        if status is not None:
            stmt = stmt.where(Order.status == status)
        if query:
            pattern = f"%{query}%"
            stmt = stmt.where(
                or_(
                    Order.number.ilike(pattern),
                    Order.email.ilike(pattern),
                    Order.phone.ilike(pattern),
                )
            )
        return stmt

    async def list_orders(
        self,
        *,
        status: str | None,
        query: str | None,
        offset: int,
        limit: int,
    ) -> list[Order]:
        """Return one page of orders (newest first) matching the filters.

        Args:
            status: Exact fulfilment status to filter by, or ``None``.
            query: Free-text search over ``number`` / ``email`` / ``phone``, or
                ``None``.
            offset: Number of rows to skip (``(page - 1) * page_size``).
            limit: Page size.

        Returns:
            list[Order]: The page of orders, newest first.
        """
        stmt = self._apply_filters(select(Order), status, query)
        stmt = stmt.order_by(Order.created_at.desc(), Order.id.desc())
        stmt = stmt.offset(offset).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_orders(
        self,
        *,
        status: str | None,
        query: str | None,
    ) -> int:
        """Return the total number of orders matching the filters.

        Args:
            status: Exact fulfilment status to filter by, or ``None``.
            query: Free-text search over ``number`` / ``email`` / ``phone``, or
                ``None``.

        Returns:
            int: Total matching row count (across all pages).
        """
        base = select(func.count()).select_from(Order)
        stmt = self._apply_filters(base, status, query)
        return int((await self.session.scalar(stmt)) or 0)
