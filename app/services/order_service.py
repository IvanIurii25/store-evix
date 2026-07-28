"""Order state-machine service (stage B6, §8).

The original system had NO enforced transition table — any code could assign any
status (§8). Here transitions are validated against explicit dictionaries; an
illegal move raises a domain error instead of silently mutating the row.

Two independent axes (§8):

* ``status`` (fulfilment): ``new → confirmed → done``; ``canceled`` from
  ``{new, confirmed}``.
* ``payment_status`` (COD): ``pending → paid``; ``paid → refunded``.

Every accepted transition appends an ``order_status_history`` row. Terminal side
effects (e.g. returning stock on cancel) run **after** ``commit`` — never in the
middle of the transaction (§8). The service commits its own unit of work.
"""

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.models.order import Order
from app.repositories.order_repo import OrderRepository

logger = logging.getLogger(__name__)

# Allowed fulfilment-status transitions (§8). Key = current, value = reachable.
STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"confirmed", "canceled"}),
    "confirmed": frozenset({"done", "canceled"}),
    "done": frozenset(),
    "canceled": frozenset(),
}

# Allowed payment-status transitions (COD, §8).
PAYMENT_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"paid"}),
    "paid": frozenset({"refunded"}),
    "refunded": frozenset(),
}

# Fulfilment statuses whose entry returns reserved stock to inventory (§8).
_STOCK_RETURNING_STATUSES: frozenset[str] = frozenset({"canceled"})


class OrderError(DomainError):
    """Base class for order domain errors (rendered via the unified envelope)."""


class IllegalTransitionError(OrderError):
    """The requested status/payment transition is not allowed by the machine."""

    status_code = status.HTTP_409_CONFLICT
    code = "illegal_transition"


class OrderService:
    """Enforced order state machine bound to one session (§8)."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and its repository.

        Args:
            session: Active async session (request-scoped or test-scoped).
        """
        self.session = session
        self.repo = OrderRepository(session)

    async def transition(
        self,
        order: Order,
        *,
        to_status: str | None = None,
        to_payment_status: str | None = None,
        changed_by: str = "system",
    ) -> Order:
        """Validate and apply a status and/or payment-status transition (§8).

        Exactly the axes requested are moved; each accepted move writes an
        ``order_status_history`` row. An illegal move raises before any mutation
        so the order is never left in an inconsistent state. Terminal side
        effects (stock return on cancel) run after ``commit``.

        Args:
            order: The order to transition (mutated in place).
            to_status: Target fulfilment status, or ``None`` to leave unchanged.
            to_payment_status: Target payment status, or ``None`` to leave
                unchanged.
            changed_by: Actor recorded in history (``system`` / ``admin`` / id).

        Returns:
            Order: The transitioned order.

        Raises:
            IllegalTransitionError: If any requested move is not allowed.
        """
        self._validate(order.status, to_status, STATUS_TRANSITIONS, "status")
        self._validate(
            order.payment_status,
            to_payment_status,
            PAYMENT_TRANSITIONS,
            "payment_status",
        )

        after_commit: list[Callable[[], Coroutine[Any, Any, None]]] = []

        if to_status is not None:
            self.repo.add_status_history(
                order_id=order.id,
                from_status=order.status,
                to_status=to_status,
                changed_by=changed_by,
            )
            if to_status in _STOCK_RETURNING_STATUSES:
                after_commit.append(self._make_stock_return(order.id))
            order.status = to_status

        if to_payment_status is not None:
            self.repo.add_status_history(
                order_id=order.id,
                from_status=order.payment_status,
                to_status=to_payment_status,
                changed_by=changed_by,
            )
            order.payment_status = to_payment_status

        await self.session.commit()
        for side_effect in after_commit:
            await side_effect()
        return order

    def _validate(
        self,
        current: str,
        target: str | None,
        table: dict[str, frozenset[str]],
        axis: str,
    ) -> None:
        """Raise if ``current → target`` is not an allowed move on ``axis``.

        Args:
            current: Current value of the axis.
            target: Requested value, or ``None`` (no-op — always valid).
            table: The transition table for this axis.
            axis: Axis name (for the error message).

        Raises:
            IllegalTransitionError: If the move is not permitted.
        """
        if target is None:
            return
        if target not in table.get(current, frozenset()):
            raise IllegalTransitionError(
                f"Illegal {axis} transition: {current} -> {target}"
            )

    def _make_stock_return(
        self,
        order_id: int,
    ) -> Callable[[], Coroutine[Any, Any, None]]:
        """Build the post-commit stock-return side effect for a canceled order.

        Returns every line's quantity to inventory via the repository's atomic
        :meth:`~app.repositories.order_repo.OrderRepository.increment_stock`, and
        for any product that thereby crosses ``0 → >0`` enqueues the restock
        notification (reusing the ``restock.notify`` Celery task, §3/§8). The
        whole closure is wrapped in ``try/except`` and kept out of the
        transaction body so a failing side effect never rolls back an
        already-committed status change (mirrors the checkout confirmation-email
        side effect). Cancel is terminal (``canceled`` has no outgoing moves), so
        this can only run once per order — no separate idempotency guard needed.

        Args:
            order_id: The canceled order id.

        Returns:
            Callable: A coroutine factory to run after commit.
        """

        async def _run() -> None:
            try:
                items = await self.repo.get_items_for_order(order_id)
                returned_lines = 0
                returned_units = 0
                for item in items:
                    if item.product_id is None and item.variant_id is None:
                        continue
                    crossed_zero = await self.repo.increment_stock(
                        item.product_id, item.qty, item.variant_id
                    )
                    returned_lines += 1
                    returned_units += item.qty
                    # Restock notifications are product-level only; variable
                    # products opt out in v1, so variant lines never notify.
                    if (
                        crossed_zero
                        and item.variant_id is None
                        and item.product_id is not None
                    ):
                        self._notify_restocked(item.product_id)
                await self.session.commit()
                logger.info(
                    "order %s canceled: returned %s unit(s) across %s line(s)",
                    order_id,
                    returned_units,
                    returned_lines,
                )
            except Exception:  # noqa: BLE001 — side effect must not break cancel
                logger.exception(
                    "order %s canceled: stock-return side effect failed", order_id
                )

        return _run

    @staticmethod
    def _notify_restocked(product_id: int) -> None:
        """Enqueue the restock task for a product that crossed ``0 → >0`` (§3).

        Reuses the same ``restock.notify`` Celery task as
        :meth:`~app.services.admin_catalog_service.AdminCatalogService._notify_if_restocked`;
        the task is imported lazily to avoid an import cycle. Enqueue failures are
        swallowed and logged so a broker outage cannot break a committed cancel.

        Args:
            product_id: The product that returned to stock on cancel.
        """
        try:
            from app.tasks.restock import send_restock_notifications

            send_restock_notifications.delay(product_id)
        except Exception:  # noqa: BLE001 — enqueue must not fail the cancel
            logger.exception(
                "restock: failed to enqueue notification for product %s", product_id
            )
