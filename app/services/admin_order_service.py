"""Admin order service: list / detail / transition for the back office (§10).

Thin business layer over the admin + storefront repositories. It reuses the
storefront :class:`~app.services.order_service.OrderService` state machine for
transitions (single source of truth for the allowed moves, §8) and the
storefront :class:`~app.repositories.order_repo.OrderRepository` for get-by-number
and line hydration — no transition table or order reads are duplicated here.

Responsibilities:

* :meth:`AdminOrderService.list_orders` — filtered, paginated order page with a
  total count, plus each order's lines fetched without an N+1.
* :meth:`AdminOrderService.get_order` — one order by number, or a domain error.
* :meth:`AdminOrderService.transition` — validate the order exists, then delegate
  the status/payment move to ``OrderService.transition``.

The service raises :class:`OrderNotFoundError` (mapped to 404 by the router) when
a number is unknown; illegal transitions surface as
:class:`~app.services.order_service.IllegalTransitionError` from the reused
machine (mapped to 409 by the router).
"""

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderItem
from app.repositories.admin_order_repo import AdminOrderRepository
from app.repositories.order_repo import OrderRepository
from app.services.order_service import OrderError, OrderService


class OrderNotFoundError(OrderError):
    """No order exists for the requested number (rendered as a 404 envelope)."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "order_not_found"


class AdminOrderService:
    """Back-office order operations bound to one session (§10)."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and its repositories.

        Args:
            session: Active async session (request-scoped or test-scoped).
        """
        self.session = session
        self.admin_repo = AdminOrderRepository(session)
        self.order_repo = OrderRepository(session)
        self.state_machine = OrderService(session)

    async def list_orders(
        self,
        *,
        status: str | None,
        query: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[tuple[Order, list[OrderItem]]], int]:
        """Return one filtered, paginated page of orders and the total count.

        Each order is paired with its lines, fetched in a single batched query
        (no per-order N+1) via the reused storefront repository.

        Args:
            status: Exact fulfilment status to filter by, or ``None``.
            query: Free-text search over ``number`` / ``email`` / ``phone``, or
                ``None``.
            page: 1-based page number.
            page_size: Page size.

        Returns:
            tuple: ``(pairs, total)`` where ``pairs`` is a list of
                ``(order, items)`` tuples and ``total`` is the count across all
                pages.
        """
        offset = (page - 1) * page_size
        orders = await self.admin_repo.list_orders(
            status=status,
            query=query,
            offset=offset,
            limit=page_size,
        )
        total = await self.admin_repo.count_orders(status=status, query=query)
        items_by_order = await self.order_repo.list_items_for_orders(
            [order.id for order in orders]
        )
        pairs = [(order, items_by_order[order.id]) for order in orders]
        return pairs, total

    async def get_order(
        self, number: str, *, for_update: bool = False
    ) -> tuple[Order, list[OrderItem]]:
        """Return one order and its lines by number, or raise 404.

        Args:
            number: The human-readable order number.
            for_update: Lock the order row so a write that follows this read
                (e.g. refund) serializes against a concurrent one. Plain detail
                reads leave it ``False``.

        Returns:
            tuple: ``(order, items)``.

        Raises:
            OrderNotFoundError: If no order has that number.
        """
        order = await self._require_order(number, for_update=for_update)
        items = (await self.order_repo.list_items_for_orders([order.id]))[order.id]
        return order, items

    async def transition(
        self,
        number: str,
        *,
        to_status: str | None = None,
        to_payment_status: str | None = None,
        changed_by: str = "admin",
    ) -> tuple[Order, list[OrderItem]]:
        """Apply a status/payment transition to an order via the shared machine.

        Validates the order exists, then delegates the move to
        ``OrderService.transition`` (which validates the transition, writes the
        ``order_status_history`` row(s), and commits). Returns the updated order
        with its lines.

        Args:
            number: The order number to transition.
            to_status: Target fulfilment status, or ``None`` to leave unchanged.
            to_payment_status: Target payment status, or ``None`` to leave
                unchanged.
            changed_by: Actor recorded in history (defaults to ``admin``).

        Returns:
            tuple: ``(order, items)`` after the transition.

        Raises:
            OrderNotFoundError: If no order has that number.
            IllegalTransitionError: If a requested move is not allowed (from the
                reused state machine).
        """
        order = await self._require_order(number, for_update=True)
        await self.state_machine.transition(
            order,
            to_status=to_status,
            to_payment_status=to_payment_status,
            changed_by=changed_by,
        )
        items = (await self.order_repo.list_items_for_orders([order.id]))[order.id]
        return order, items

    async def _require_order(self, number: str, *, for_update: bool = False) -> Order:
        """Load an order by number or raise :class:`OrderNotFoundError`.

        Args:
            number: The order number.
            for_update: Lock the row for a concurrent-safe transition (§8); reads
                (order detail) leave it ``False``.

        Returns:
            Order: The found order.

        Raises:
            OrderNotFoundError: If no order has that number.
        """
        order = await self.order_repo.get_order_by_number(number, for_update=for_update)
        if order is None:
            raise OrderNotFoundError(f"Order not found: {number}")
        return order
