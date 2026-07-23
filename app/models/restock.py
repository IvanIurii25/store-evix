"""Restock-notification domain ORM model (Phase 1).

A customer subscribes to be emailed when an out-of-stock product returns to
stock. One subscription per ``(product_id, user_id)`` (the ``UNIQUE`` constraint);
re-subscribing after a notification reactivates the same row (``notified`` →
``active``, ``notified_at`` cleared).

Conventions mirror the catalog/content-page models (§2.1.1):

* ``lang`` is plain text constrained to :data:`ALLOWED_LANGS` via a
  ``CheckConstraint`` (no PostgreSQL ENUM type) — the storefront language at
  subscribe time, used to render the email.
* ``status`` is likewise a ``CheckConstraint``-guarded text column.
* Both FKs are ``ON DELETE CASCADE`` so deleting a product or an account drops
  its pending subscriptions.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# Supported languages for v1 (ru + ro). Applied as a CheckConstraint on the
# ``lang`` column instead of a PostgreSQL ENUM (mirrors catalog §2.1.1).
ALLOWED_LANGS: tuple[str, ...] = ("ru", "ro")

# Rendered SQL fragment for the ``lang`` CheckConstraint, e.g. ``'ru', 'ro'``.
_LANG_IN = ", ".join(f"'{lang}'" for lang in ALLOWED_LANGS)

# Lifecycle states. ``active`` = awaiting restock; ``notified`` = email sent
# (spent — a fresh subscription is needed to be alerted again — §8.3).
STATUS_ACTIVE: str = "active"
STATUS_NOTIFIED: str = "notified"
ALLOWED_STATUSES: tuple[str, ...] = (STATUS_ACTIVE, STATUS_NOTIFIED)
_STATUS_IN = ", ".join(f"'{status}'" for status in ALLOWED_STATUSES)


class RestockSubscription(Base, TimestampMixin):
    """A customer's request to be emailed when a product is back in stock (§2)."""

    __tablename__ = "restock_subscription"
    __table_args__ = (
        UniqueConstraint("product_id", "user_id"),
        Index("ix_restock_subscription_product_status", "product_id", "status"),
        Index("ix_restock_subscription_user", "user_id"),
        CheckConstraint(f"lang IN ({_LANG_IN})", name="lang_allowed"),
        CheckConstraint(f"status IN ({_STATUS_IN})", name="status_allowed"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("product.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Storefront language at subscribe time — drives the email language.
    lang: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default=STATUS_ACTIVE,
    )
    notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
