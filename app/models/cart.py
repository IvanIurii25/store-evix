"""Cart domain models: :class:`Cart` and :class:`CartItem` (§2.3).

Guest carts carry a ``session_token`` (cookie) and a null ``user_id``; on login
the guest cart is merged into the user's cart. Money is NOT stored on the cart —
line prices come from ``product`` on render; a snapshot is taken only at checkout
(order_item). ``product_id`` is a cross-domain FK to the catalog ``product`` table
(referenced by string to avoid an import cycle).
"""

from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

CART_STATUSES: tuple[str, ...] = ("draft", "converted", "abandoned")


class Cart(Base, TimestampMixin):
    """Shopping cart; one active (``draft``) cart per user/guest session (§2.3)."""

    __tablename__ = "cart"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'converted', 'abandoned')",
            name="cart_status_valid",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("app_user.id"),
        nullable=True,
    )
    session_token: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")


class CartItem(Base):
    """Line item in a :class:`Cart`; price is NOT stored here (§2.3)."""

    __tablename__ = "cart_item"
    __table_args__ = (
        UniqueConstraint("cart_id", "product_id", name="cart_item_cart_product"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cart_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cart.id"),
        nullable=False,
    )
    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("product.id"),
        nullable=False,
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
