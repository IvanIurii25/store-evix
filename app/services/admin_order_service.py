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
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import DomainError
from app.models.order import Order, OrderDeliveryNovaPost, OrderItem
from app.repositories.admin_order_repo import AdminOrderRepository
from app.repositories.order_repo import OrderRepository
from app.services.delivery.novapost_client import NovaPostError
from app.services.delivery.novapost_service import NovaPostService
from app.services.order_service import (
    OrderNotFoundError,
    OrderService,
)

# Re-exported for the admin router / tests that import it from here; the
# canonical definition lives next to the order state machine.
__all__ = [
    "AdminOrderService",
    "OrderNotFoundError",
    "WaybillError",
    "WaybillExistsError",
    "WaybillMissingError",
    "CarrierUnavailableError",
]


class WaybillError(DomainError):
    """Base class for back-office waybill failures."""


class WaybillExistsError(WaybillError):
    """A waybill already exists for this order.

    Mapped to HTTP 409 with code ``awb_already_exists``. The guard is what makes
    creation idempotent: a double click must not buy two shipments.
    """

    status_code = status.HTTP_409_CONFLICT
    code = "awb_already_exists"


class WaybillMissingError(WaybillError):
    """The order has no carrier shipment to act on.

    Mapped to HTTP 404 with code ``awb_not_found`` — covers both an own-logistics
    order and a carrier order whose waybill was never created.
    """

    status_code = status.HTTP_404_NOT_FOUND
    code = "awb_not_found"


class CarrierUnavailableError(WaybillError):
    """The carrier refused or could not be reached.

    Mapped to HTTP 502 with code ``carrier_unavailable``. Nothing is written: an
    operator retries rather than being told a waybill exists when it does not.
    """

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "carrier_unavailable"


class AdminOrderService:
    """Back-office order operations bound to one session (§10)."""

    def __init__(self, session: AsyncSession, redis: "Redis | None" = None) -> None:
        """Bind the service to a session and its repositories.

        Args:
            session: Active async session (request-scoped or test-scoped).
        """
        self.session = session
        # Only the carrier operations need it; every other back-office use-case
        # works without Redis, and so does every existing test.
        self.redis = redis
        self.admin_repo = AdminOrderRepository(session)
        self.order_repo = OrderRepository(session)
        self.state_machine = OrderService(session, redis)

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

    async def novapost(self, order_id: int) -> "OrderDeliveryNovaPost | None":
        """Return the carrier row for one order (back-office detail view)."""
        return await self.order_repo.get_novapost(order_id)

    async def novapost_map(
        self, order_ids: list[int]
    ) -> dict[int, "OrderDeliveryNovaPost"]:
        """Return carrier rows for a page of orders, keyed by order id."""
        return await self.order_repo.get_novapost_map(order_ids)

    # ------------------------------------------------------------------ #
    # Waybills (Nova Post)
    # ------------------------------------------------------------------ #
    async def create_waybill(self, number: str) -> OrderDeliveryNovaPost:
        """Create the carrier waybill for one order (idempotent).

        The carrier row is locked ``FOR UPDATE`` for the whole operation, so two
        operators clicking at once cannot buy two shipments for one order: the
        second waits, then sees the number the first wrote and is refused.

        Args:
            number: The order number.

        Returns:
            OrderDeliveryNovaPost: The updated carrier row.

        Raises:
            OrderNotFoundError: If no order has that number.
            WaybillMissingError: If the order does not ship with the carrier.
            WaybillExistsError: If a waybill was already created.
            CarrierUnavailableError: If the carrier refuses or is unreachable.
        """
        order, row = await self._lock_carrier_row(number)
        if row.awb_number:
            raise WaybillExistsError(f"Order {number} already has a waybill")

        items = (await self.order_repo.list_items_for_orders([order.id]))[order.id]
        weights = await self._parcel_weights(row, items)
        service = self._carrier()
        payload = service.build_shipment(
            order_number=order.number,
            recipient_name=order.delivery_name or "",
            phone=order.phone,
            email=order.email,
            delivery_type=order.delivery_type,
            division_id=row.division_id,
            settlement_id=row.settlement_id,
            address_parts=row.address_parts,
            weights_g=weights,
            insurance=order.subtotal - order.discount_total,
        )
        try:
            response = await service.create_shipment(payload)
        except NovaPostError as exc:
            # Nothing is written: telling an operator a waybill exists when it
            # does not is worse than making them retry.
            raise CarrierUnavailableError(str(exc)) from exc

        row.awb_data = response
        row.awb_id = str(response.get("id") or "") or None
        row.awb_number = str(response.get("number") or "") or None
        row.status_code = ""
        row.status_text = str(response.get("status") or "")
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def cancel_waybill(self, number: str) -> OrderDeliveryNovaPost:
        """Cancel the carrier waybill for one order.

        Args:
            number: The order number.

        Returns:
            OrderDeliveryNovaPost: The carrier row with the waybill cleared.

        Raises:
            OrderNotFoundError: If no order has that number.
            WaybillMissingError: If there is no waybill to cancel.
            CarrierUnavailableError: If the carrier refuses or is unreachable.
        """
        _order, row = await self._lock_carrier_row(number)
        if not row.awb_number:
            raise WaybillMissingError(f"Order {number} has no waybill")
        try:
            await self._carrier().cancel_shipment(row.awb_id or row.awb_number)
        except NovaPostError as exc:
            # Clearing our copy while the parcel still travels would be worse
            # than leaving the operator to retry.
            raise CarrierUnavailableError(str(exc)) from exc
        row.awb_id = None
        row.awb_number = None
        row.status_code = ""
        row.status_text = ""
        row.status_updated_at = None
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def _lock_carrier_row(
        self, number: str
    ) -> tuple[Order, OrderDeliveryNovaPost]:
        """Load an order and lock its carrier row for update."""
        order = await self.order_repo.get_order_by_number(number)
        if order is None:
            raise OrderNotFoundError(f"Order not found: {number}")
        row = (
            await self.session.execute(
                select(OrderDeliveryNovaPost)
                .where(OrderDeliveryNovaPost.order_id == order.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise WaybillMissingError(f"Order {number} is not a Nova Post order")
        return order, row

    async def _parcel_weights(
        self, row: OrderDeliveryNovaPost, items: list[OrderItem]
    ) -> list[int]:
        """Return the per-item weights the waybill should declare.

        Prefers the snapshot taken at checkout — that is what the customer was
        quoted for. Rows created before the snapshot existed fall back to the
        current catalogue, which is the best available answer.
        """
        if row.parcel_weight_g:
            return [row.parcel_weight_g]
        default = settings.parcel_default_item_weight_g
        product_ids = [item.product_id for item in items if item.product_id]
        weights = await self.order_repo.get_product_weights(product_ids)
        return [(weights.get(item.product_id) or default) * item.qty for item in items]

    def _carrier(self) -> "NovaPostService":
        """Build the carrier service, or refuse when it is not configured."""
        if self.redis is None or not settings.novapost_enabled:
            raise CarrierUnavailableError("Nova Post is not configured")
        return NovaPostService(self.redis)
