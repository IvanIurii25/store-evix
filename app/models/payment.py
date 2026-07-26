"""Payment domain model: :class:`Payment` (card / maib audit + idempotency).

One row per payment attempt against an :class:`~app.models.order.Order`. It is
the audit trail for card payments and the idempotency anchor for the maib
callback (looked up by the unique ``pay_id``): a repeated callback for an already
terminal payment is a no-op. COD orders never create a payment row — the card
path is fully additive and env-gated (see ``settings.card_payment_enabled``).

``raw`` snapshots the last provider payload (callback ``result`` or pay-info) for
support/debugging; it carries no money authority (the order state machine does).
"""

from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# Provider identifiers (only maib in the MVP; column kept for future PSPs).
PAYMENT_PROVIDERS: tuple[str, ...] = ("maib",)

# Local payment lifecycle. ``pending`` at creation; ``ok`` / ``failed`` set from
# the maib callback; ``refunded`` after a successful refund. Kept distinct from
# ``order.payment_status`` (the order remains the money source of truth).
PAYMENT_STATUSES: tuple[str, ...] = ("pending", "ok", "failed", "refunded")


class Payment(Base, TimestampMixin):
    """A card payment attempt against an order (maib provider)."""

    __tablename__ = "payment"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('maib')",
            name="payment_provider_valid",
        ),
        CheckConstraint(
            "status IN ('pending', 'ok', 'failed', 'refunded')",
            name="payment_status_valid",
        ),
        Index("ix_payment_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("order.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="maib",
    )
    # maib transaction id; unique for callback idempotency. Null until ``/pay``
    # returns (the row is created ``pending`` first).
    pay_id: Mapped[str | None] = mapped_column(
        String,
        unique=True,
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="pending",
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="MDL")
    # Last provider payload snapshot (callback result / pay-info) for support.
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
