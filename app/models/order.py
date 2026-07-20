"""Order domain models: :class:`Order`, :class:`OrderItem`,
:class:`OrderStatusHistory` (§2.4, §8, §11).

Two independent status axes (§8): ``status`` (fulfilment: new → confirmed → done,
or canceled) and ``payment_status`` (COD: pending → paid → refunded). Line items
snapshot ``name``/``price`` at order time and use ``ON DELETE SET NULL`` on
``product_id`` so orders survive product deletion (§11). ``promo_code`` is a soft
link (text snapshot of the code), NOT an FK — deactivating a promo must not break
order history (ERD notes).
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CITEXT, Base, TimestampMixin

ORDER_STATUSES: tuple[str, ...] = ("new", "confirmed", "done", "canceled")
PAYMENT_STATUSES: tuple[str, ...] = ("pending", "paid", "refunded")
DELIVERY_TYPES: tuple[str, ...] = ("pickup", "courier")


class Order(Base, TimestampMixin):
    """Customer order; ``__tablename__`` is the reserved word ``order`` (quoted)."""

    __tablename__ = "order"
    __table_args__ = (
        CheckConstraint(
            "status IN ('new', 'confirmed', 'done', 'canceled')",
            name="order_status_valid",
        ),
        CheckConstraint(
            "payment_status IN ('pending', 'paid', 'refunded')",
            name="order_payment_status_valid",
        ),
        CheckConstraint(
            "delivery_type IN ('pickup', 'courier')",
            name="order_delivery_type_valid",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    number: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("app_user.id"),
        nullable=True,
    )
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    phone: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="new")
    payment_status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending"
    )
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    discount_total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal("0"),
    )
    delivery_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        default=Decimal("0"),
    )
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    delivery_type: Mapped[str] = mapped_column(String, nullable=False)
    delivery_address_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("address.id"),
        nullable=True,
    )
    # Delivery address snapshot (courier). Captured at checkout so the order is
    # immutable even if a saved address is later edited/deleted, and so guests
    # (no saved address) can use courier. Null for pickup.
    delivery_name: Mapped[str | None] = mapped_column(String, nullable=True)
    delivery_city: Mapped[str | None] = mapped_column(String, nullable=True)
    delivery_street: Mapped[str | None] = mapped_column(String, nullable=True)
    delivery_zip: Mapped[str | None] = mapped_column(String, nullable=True)
    payment_method: Mapped[str] = mapped_column(String, nullable=False, default="cod")
    promo_code: Mapped[str | None] = mapped_column(String, nullable=True)


class OrderItem(Base):
    """Order line with snapshotted name/price; ``product_id`` SET NULL (§11)."""

    __tablename__ = "order_item"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("order.id"),
        nullable=False,
    )
    product_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("product.id", ondelete="SET NULL"),
        nullable=True,
    )
    name_snapshot: Mapped[str] = mapped_column(String, nullable=False)
    price_snapshot: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)


class OrderStatusHistory(Base):
    """Audit record of an order status transition (§8)."""

    __tablename__ = "order_status_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("order.id"),
        nullable=False,
    )
    from_status: Mapped[str] = mapped_column(String, nullable=False)
    to_status: Mapped[str] = mapped_column(String, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    changed_by: Mapped[str] = mapped_column(String, nullable=False)
