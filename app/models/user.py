"""User domain models: :class:`AppUser` and :class:`Address`.

Single-tenant (§2.2): no company/B2B/tenant/agent fields. ``email`` is the login
identifier (case-insensitive via :class:`CITEXT`). ``loyalty_points`` is optional
promo bookkeeping (§2.5).
"""

from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CITEXT, Base, TimestampMixin


class AppUser(Base, TimestampMixin):
    """Registered customer / staff account (§2.2)."""

    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_staff: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    loyalty_points: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal("0"),
    )


class Address(Base):
    """Delivery/contact address owned by an :class:`AppUser` (§2.2)."""

    __tablename__ = "address"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("app_user.id"),
        nullable=False,
    )
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str] = mapped_column(String, nullable=False)
    city: Mapped[str] = mapped_column(String, nullable=False)
    street: Mapped[str] = mapped_column(String, nullable=False)
    zip: Mapped[str | None] = mapped_column(String, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
