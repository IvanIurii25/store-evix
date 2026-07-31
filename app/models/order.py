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
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CITEXT, Base, TimestampMixin

ORDER_STATUSES: tuple[str, ...] = ("new", "confirmed", "done", "canceled")
PAYMENT_STATUSES: tuple[str, ...] = ("pending", "paid", "refunded")
# How the parcel reaches the customer. ``branch``/``postomat`` are carrier
# pickup points; the pair (service, type) identifies a method, so ``courier``
# means our own courier under ``own`` and the carrier's under ``novapost``.
DELIVERY_TYPES: tuple[str, ...] = ("pickup", "courier", "branch", "postomat")
# Who delivers it.
DELIVERY_SERVICES: tuple[str, ...] = ("own", "novapost")


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
            "delivery_type IN ('pickup', 'courier', 'branch', 'postomat')",
            name="order_delivery_type_valid",
        ),
        CheckConstraint(
            "delivery_service IN ('own', 'novapost')",
            name="order_delivery_service_valid",
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
    # Which logistics provider fulfils it; ``own`` keeps the pre-carrier
    # behaviour (pickup / flat-rate courier) for every historical order.
    delivery_service: Mapped[str] = mapped_column(
        String, nullable=False, default="own", server_default="own"
    )
    delivery_address_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("address.id", ondelete="SET NULL"),
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
    # Soft link to the purchased variant (variable products); SET NULL so orders
    # survive variant deletion. The chosen options are also baked into
    # ``name_snapshot`` for human-readable history even if the variant is gone.
    variant_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("product_variant.id", ondelete="SET NULL"),
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


class OrderDeliveryNovaPost(Base):
    """Carrier-side data for one Nova Post order (1:1 with :class:`Order`).

    Kept in its own table rather than as columns on ``order``: it only exists
    for carrier orders, it carries a JSON address and waybill payload, and its
    lifecycle (tracking updates) is independent of the order's own status axes.

    The settlement / division fields are a **snapshot**, deliberately duplicating
    what the carrier could return. An order has to stay readable long after a
    branch is renamed or the integration is switched off — the same reason the
    delivery address is snapshotted onto ``order`` itself.
    """

    __tablename__ = "order_delivery_np"

    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("order.id", ondelete="CASCADE"),
        primary_key=True,
    )
    settlement_id: Mapped[str | None] = mapped_column(String, nullable=True)
    settlement_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    # Second snapshot in the other locale: the carrier localizes names, and the
    # back-office reads orders in Russian while the storefront may have been ro.
    settlement_name_ru: Mapped[str] = mapped_column(String, nullable=False, default="")
    division_id: Mapped[str | None] = mapped_column(String, nullable=True)
    division_number: Mapped[str] = mapped_column(String, nullable=False, default="")
    division_address: Mapped[str] = mapped_column(String, nullable=False, default="")
    # Courier-to-the-door address in the carrier's own field layout.
    address_parts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # What the carrier quoted; ``order.delivery_cost`` may be 0 instead when a
    # free-delivery threshold applied, so both are worth keeping.
    calculated_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    awb_id: Mapped[str | None] = mapped_column(String, nullable=True)
    awb_number: Mapped[str | None] = mapped_column(String, nullable=True)
    awb_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Machine-readable code plus the carrier's own wording. ecom-obr stores a
    # single formatted string ("(10) | Accepted"), which cannot be filtered on.
    status_code: Mapped[str] = mapped_column(String, nullable=False, default="")
    status_text: Mapped[str] = mapped_column(String, nullable=False, default="")
    status_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
