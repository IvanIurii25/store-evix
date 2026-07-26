"""Async data-access layer for card payments (maib).

Only SQL / query construction lives here — no business rules (those belong to
:class:`~app.services.payment.payment_service.PaymentService`) and no HTTP.
Committing is the caller's (service's) responsibility, matching the rest of the
repository layer.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import Payment


class PaymentRepository:
    """Payment table access bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a session.

        Args:
            session: Active async session (request-scoped or test-scoped).
        """
        self.session = session

    def add_payment(
        self,
        *,
        order_id: int,
        amount: Decimal,
        currency: str = "MDL",
        provider: str = "maib",
        status: str = "pending",
    ) -> Payment:
        """Stage a new ``pending`` payment row for an order (no commit).

        Args:
            order_id: Owning order id.
            amount: Payment amount.
            currency: ISO currency (default ``MDL``).
            provider: Payment provider (default ``maib``).
            status: Initial local status (default ``pending``).

        Returns:
            Payment: The staged (not yet flushed) payment.
        """
        payment = Payment(
            order_id=order_id,
            amount=amount,
            currency=currency,
            provider=provider,
            status=status,
        )
        self.session.add(payment)
        return payment

    async def get_by_pay_id(self, pay_id: str) -> Payment | None:
        """Load a payment by its maib ``pay_id`` (callback idempotency anchor).

        Args:
            pay_id: The maib transaction id.

        Returns:
            Payment | None: The payment, or ``None``.
        """
        stmt = select(Payment).where(Payment.pay_id == pay_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_order_id(self, order_id: int) -> Payment | None:
        """Load the (single) payment for an order, if any.

        Args:
            order_id: The order id.

        Returns:
            Payment | None: The most recent payment for the order, or ``None``.
        """
        stmt = (
            select(Payment)
            .where(Payment.order_id == order_id)
            .order_by(Payment.id.desc())
        )
        return (await self.session.execute(stmt)).scalars().first()
