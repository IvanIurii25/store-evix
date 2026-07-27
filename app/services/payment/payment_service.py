"""Card-payment orchestration (maib): initiate, callback, refund.

Sits between the checkout / webhook / admin routers and both the
:class:`~app.services.payment.maib_client.MaibClient` (transport) and the order
state machine (:class:`~app.services.order_service.OrderService`, the money
source of truth). It creates the ``payment`` audit row, drives the maib ``/pay``
call, and — on the signed callback — moves the order via the *existing*
transitions only:

* callback ``status == "OK"`` → ``payment_status = paid``;
* any other terminal callback → order ``canceled`` (which returns stock via the
  state machine's post-commit side effect).

The callback is idempotent on ``pay_id``: a repeat for an already-terminal
payment is a no-op, so maib's at-least-once delivery never double-applies.
"""

import logging
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.order import Order
from app.repositories.order_repo import OrderRepository
from app.repositories.payment_repo import PaymentRepository
from app.services.order_service import IllegalTransitionError, OrderService
from app.services.payment.maib_client import MaibClient, MaibError

logger = logging.getLogger(__name__)

# maib "success" marker in the callback ``result.status`` field.
_MAIB_OK: str = "OK"

# Local payment statuses (mirror app.models.payment.PAYMENT_STATUSES).
_STATUS_OK: str = "ok"
_STATUS_FAILED: str = "failed"
_STATUS_REFUNDED: str = "refunded"

# Terminal local statuses that make a repeated callback a no-op.
_TERMINAL_STATUSES: frozenset[str] = frozenset({_STATUS_OK, _STATUS_FAILED, _STATUS_REFUNDED})


class PaymentError(Exception):
    """Base class for card-payment domain errors (mapped to HTTP by routers)."""


class CardPaymentDisabledError(PaymentError):
    """Card payment was requested while ``card_payment_enabled`` is false."""


class PaymentNotFoundError(PaymentError):
    """No card payment exists for the given order (refund with nothing to refund)."""


class RefundNotAllowedError(PaymentError):
    """The order is not a paid card order, so it cannot be refunded."""


class PaymentService:
    """Card-payment use-cases bound to one session."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        client: MaibClient | None = None,
    ) -> None:
        """Bind the service to a session, its repositories and a maib client.

        Args:
            session: Active async session (request-scoped or test-scoped).
            client: maib transport client; a default one is built when omitted
                (tests inject a stub/mocked client here).
        """
        self.session = session
        self.payment_repo = PaymentRepository(session)
        self.order_repo = OrderRepository(session)
        self.state_machine = OrderService(session)
        self.client = client if client is not None else MaibClient()

    # ------------------------------------------------------------------ #
    # Initiate (checkout card flow)
    # ------------------------------------------------------------------ #
    async def initiate_card_payment(
        self,
        order: Order,
        *,
        client_ip: str,
        lang: str,
    ) -> str:
        """Create a ``payment`` row and a maib payment; return the ``payUrl``.

        The order is already persisted (pending, stock decremented) by checkout.
        This stages a ``pending`` payment, calls maib ``/pay`` with the order
        number as ``orderId`` and our webhook as ``callbackUrl``, stores the
        returned ``pay_id`` and commits.

        Args:
            order: The just-created pending order.
            client_ip: Payer IP (fraud / 3-DS signal for maib).
            lang: Checkout-page language (``ro`` | ``ru`` | ``en``).

        Returns:
            str: The maib ``payUrl`` the storefront redirects the payer to.

        Raises:
            CardPaymentDisabledError: If card payment is not enabled.
            MaibError: If the maib ``/pay`` call fails (caller maps to 5xx).
        """
        if not settings.card_payment_enabled:
            raise CardPaymentDisabledError("Card payment is not enabled")

        payment = self.payment_repo.add_payment(
            order_id=order.id,
            amount=order.total,
            currency=settings.currency,
        )
        await self.session.flush()

        try:
            result = await self.client.create_payment(
                amount=float(order.total),
                order_id=order.number,
                client_ip=client_ip,
                language=lang,
                description=f"Order {order.number}",
                currency=settings.currency,
                callback_url=self._callback_url(),
                ok_url=self._return_url(lang, order.number, success=True),
                fail_url=self._return_url(lang, order.number, success=False),
                email=order.email,
                client_name=order.delivery_name,
            )
        except MaibError:
            # Leave the payment row ``pending`` for audit; surface to the caller.
            await self.session.commit()
            raise

        payment.pay_id = result["payId"]
        payment.raw = result
        await self.session.commit()
        return result["payUrl"]

    # ------------------------------------------------------------------ #
    # Callback (webhook)
    # ------------------------------------------------------------------ #
    async def handle_callback(self, result: dict, signature: str) -> bool:
        """Verify + apply a maib callback idempotently; return whether accepted.

        Steps: verify the signature (invalid → reject, no order change); find the
        payment by ``pay_id`` (unknown → reject); if the payment is already
        terminal, no-op (idempotent). Otherwise move the order via the existing
        state machine — ``OK`` → ``payment_status = paid``; anything else → order
        ``canceled`` (returns stock) — and stamp the local payment status.

        Args:
            result: The ``result`` block from the callback (verbatim dict).
            signature: The ``signature`` field from the callback.

        Returns:
            bool: True if the signature was valid and the callback was processed
                (including an idempotent no-op); False if it was rejected.
        """
        if not self.client.verify_callback_signature(result, signature):
            logger.warning("maib callback: invalid signature, ignoring")
            return False

        pay_id = result.get("payId")
        if not pay_id:
            logger.warning("maib callback: missing payId, ignoring")
            return False

        payment = await self.payment_repo.get_by_pay_id(str(pay_id))
        if payment is None:
            logger.warning("maib callback: unknown payId %s, ignoring", pay_id)
            return False

        if payment.status in _TERMINAL_STATUSES:
            logger.info(
                "maib callback: payment %s already %s, no-op",
                pay_id,
                payment.status,
            )
            return True

        order = await self.order_repo.get_order_by_id(
            payment.order_id, for_update=True
        )
        if order is None:  # pragma: no cover - FK guarantees the order exists
            logger.error("maib callback: order %s vanished", payment.order_id)
            return False

        payment.raw = result
        maib_status = str(result.get("status", "")).upper()
        if maib_status == _MAIB_OK:
            await self._apply_paid(order, payment)
        else:
            await self._apply_failed(order, payment)
        return True

    async def _apply_paid(self, order: Order, payment) -> None:  # noqa: ANN001
        """Mark the payment ``ok`` and move the order to ``paid`` (idempotent).

        Args:
            order: The order to settle.
            payment: The payment row (mutated in place).
        """
        payment.status = _STATUS_OK
        self.session.add(payment)
        if order.payment_status == "pending":
            await self.state_machine.transition(
                order,
                to_payment_status="paid",
                changed_by="maib",
            )
        else:
            await self.session.commit()

    async def _apply_failed(self, order: Order, payment) -> None:  # noqa: ANN001
        """Mark the payment ``failed`` and cancel the order (returns stock).

        Cancelling goes through the existing state machine, whose post-commit
        side effect returns reserved stock. An order already terminal (e.g.
        already canceled) simply commits the payment status without a transition.

        Args:
            order: The order to cancel.
            payment: The payment row (mutated in place).
        """
        payment.status = _STATUS_FAILED
        self.session.add(payment)
        if order.status in ("new", "confirmed"):
            try:
                await self.state_machine.transition(
                    order,
                    to_status="canceled",
                    changed_by="maib",
                )
            except IllegalTransitionError:  # pragma: no cover - guarded above
                await self.session.commit()
        else:
            await self.session.commit()

    # ------------------------------------------------------------------ #
    # Refund (admin)
    # ------------------------------------------------------------------ #
    async def refund(self, order: Order, amount: Decimal | None = None) -> None:
        """Refund a paid card order via maib and mark it ``refunded``.

        Args:
            order: The paid card order to refund.
            amount: Optional partial amount; ``None`` refunds in full.

        Raises:
            RefundNotAllowedError: If the order is not a paid card order.
            PaymentNotFoundError: If the order has no card payment with a pay_id.
            MaibError: If the maib refund call fails.
        """
        if order.payment_method != "card" or order.payment_status != "paid":
            raise RefundNotAllowedError(
                "Only a paid card order can be refunded"
            )
        payment = await self.payment_repo.get_by_order_id(order.id)
        if payment is None or payment.pay_id is None:
            raise PaymentNotFoundError("No card payment to refund for this order")

        await self.client.refund(
            payment.pay_id,
            amount=float(amount) if amount is not None else None,
        )
        payment.status = _STATUS_REFUNDED
        self.session.add(payment)
        await self.state_machine.transition(
            order,
            to_payment_status="refunded",
            changed_by="admin",
        )

    # ------------------------------------------------------------------ #
    # URL builders
    # ------------------------------------------------------------------ #
    @staticmethod
    def _callback_url() -> str:
        """Build the maib callback URL for this API instance.

        Returns:
            str: ``<maib_callback_base_url>/api/v1/payments/maib/callback``.
        """
        base = settings.maib_callback_base_url.rstrip("/")
        return f"{base}/api/v1/payments/maib/callback"

    @staticmethod
    def _return_url(lang: str, order_number: str, *, success: bool) -> str:
        """Build the storefront ok/fail return URL for a language + order.

        Args:
            lang: Storefront language segment.
            order_number: The order number, passed back for display.
            success: Whether this is the success (vs failed) return URL.

        Returns:
            str: ``<storefront>/{lang}/checkout/{success|failed}?order=<number>``.
        """
        base = settings.storefront_base_url.rstrip("/")
        page = "success" if success else "failed"
        return f"{base}/{lang}/checkout/{page}?order={order_number}"
