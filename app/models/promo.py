"""Promo domain model: :class:`PromoCode` (§2.5).

Discount is either ``percent`` or ``fixed``. Validity window (``active_from`` /
``active_to``), optional minimum order total and usage limit. Orders reference the
code by value (soft link), so this table has no inbound FK from ``order``.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

DISCOUNT_TYPES: tuple[str, ...] = ("percent", "fixed")


class PromoCode(Base):
    """Discount coupon applied at checkout (§2.5)."""

    __tablename__ = "promo_code"
    __table_args__ = (
        CheckConstraint(
            "discount_type IN ('percent', 'fixed')",
            name="promo_code_discount_type_valid",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    discount_type: Mapped[str] = mapped_column(String, nullable=False)
    discount_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    active_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    active_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    min_order_total: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    usage_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
