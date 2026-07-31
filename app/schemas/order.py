"""Pydantic v2 schemas for the checkout + order domain (stage B6, §9 / §4).

Request bodies: :class:`QuoteRequest` (predraft, no order created) and
:class:`CheckoutRequest` (creates the order). Responses: a computed
:class:`QuoteOut` (delivery / discount / total breakdown) and the persisted
:class:`OrderOut` with its :class:`OrderItemOut` lines.

Money fields are ``Decimal`` (mapped from ``numeric(12,2)`` — MDL, §2.6). These
are narrow DTOs (only the fields the storefront needs), not god-DTOs.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.order import (
    DELIVERY_TYPES,
    Order,
    OrderDeliveryNovaPost,
    OrderItem,
)

# Rendered literal set for the ``delivery_type`` field description.
_DELIVERY_CHOICES: str = " | ".join(DELIVERY_TYPES)


class DeliveryAddressIn(BaseModel):
    """Inline courier address (guest or user), snapshotted onto the order so it
    survives independently of any saved address (§2.4 snapshot pattern)."""

    full_name: str = Field(min_length=1, max_length=255)
    city: str = Field(min_length=1, max_length=255)
    street: str = Field(min_length=1, max_length=255)
    zip: str | None = Field(default=None, max_length=32)


class NovaPostAddressIn(BaseModel):
    """Courier-to-the-door address in the carrier's own field layout.

    Mirrors ``ADDRESS_FIELDS`` in :mod:`app.schemas.delivery`, which the
    storefront builds its form from — the two must stay in step, so the required
    fields here are the ones advertised there.
    """

    city: str = Field(min_length=1, max_length=100)
    street: str = Field(min_length=1, max_length=100)
    building: str = Field(min_length=1, max_length=100)
    postCode: str = Field(min_length=1, max_length=10)  # noqa: N815 — carrier field
    flat: str | None = Field(default=None, max_length=10)
    block: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=100)


class _DeliverySelection(BaseModel):
    """Delivery fields shared by the quote and the checkout request."""

    delivery_service: str = Field(
        default="own",
        description="Who delivers: own | novapost.",
    )
    np_settlement_id: str | None = Field(
        default=None,
        max_length=64,
        description="Carrier city id (Nova Post courier).",
    )
    np_division_id: str | None = Field(
        default=None,
        max_length=64,
        description="Carrier pickup-point id (Nova Post branch / postomat).",
    )
    np_address: NovaPostAddressIn | None = Field(
        default=None,
        description="Carrier courier address.",
    )
    np_recipient_name: str | None = Field(
        default=None,
        max_length=255,
        description="Who collects the parcel (required for Nova Post).",
    )


class QuoteRequest(_DeliverySelection):
    """Request body for ``POST /checkout/quote`` — a pure predraft (§9)."""

    delivery_type: str = Field(
        ...,
        description=f"Delivery method ({_DELIVERY_CHOICES}).",
    )
    delivery_address_id: int | None = Field(
        default=None,
        gt=0,
        description="Saved address id (courier; logged-in users).",
    )
    delivery_address: DeliveryAddressIn | None = Field(
        default=None,
        description="Inline courier address (guest or user). Alternative to id.",
    )
    promo_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Optional coupon code to apply to the subtotal.",
    )


class CheckoutRequest(_DeliverySelection):
    """Request body for ``POST /checkout`` — creates the order (§9).

    ``email`` + ``phone`` are always required (guest checkout is allowed, §9); an
    authenticated caller's ``user_id`` is taken from the token, not the body.
    """

    email: str = Field(..., min_length=3, description="Contact email (guest + user).")
    phone: str = Field(..., min_length=3, description="Contact phone (guest + user).")
    delivery_type: str = Field(
        ...,
        description=f"Delivery method ({_DELIVERY_CHOICES}).",
    )
    delivery_address_id: int | None = Field(
        default=None,
        gt=0,
        description="Saved address id (courier; logged-in users).",
    )
    delivery_address: DeliveryAddressIn | None = Field(
        default=None,
        description="Inline courier address (guest or user). Alternative to id.",
    )
    promo_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Optional coupon code; re-validated server-side at order time.",
    )
    payment_method: str = Field(
        default="cod",
        description="Payment method: 'cod' (default) or 'card' (maib). 'card' "
        "is accepted only when card payment is enabled server-side.",
    )


class QuickBuyRequest(BaseModel):
    """Request body for ``POST /checkout/quick`` — one-click COD buy (feature A2).

    A phone-only flow: the guest supplies a ``product_id`` and a ``phone`` and
    an operator calls back. ``email`` is optional (the ``order.email`` column is
    NOT NULL, so a placeholder is derived from the phone when it is omitted);
    ``name`` is optional and snapshotted onto the order's ``delivery_name``.
    Delivery is always free pickup — no address, no cart, no full checkout.
    """

    product_id: int = Field(..., gt=0, description="Product to buy in one click.")
    variant_id: int | None = Field(
        default=None, gt=0, description="Chosen variant id (variable products)."
    )
    phone: str = Field(..., min_length=3, description="Contact phone (required).")
    name: str | None = Field(
        default=None,
        max_length=255,
        description="Optional customer name (snapshotted as delivery_name).",
    )
    email: str | None = Field(
        default=None,
        min_length=3,
        description="Optional contact email; a placeholder is derived from the "
        "phone when omitted (the order email column is NOT NULL).",
    )
    qty: int = Field(default=1, gt=0, description="Quantity (defaults to 1).")


class QuoteOut(BaseModel):
    """Computed checkout totals returned by ``POST /checkout/quote`` (§9)."""

    subtotal: Decimal
    discount_total: Decimal
    delivery_cost: Decimal
    total: Decimal
    delivery_type: str
    delivery_service: str = "own"
    item_count: int


class NovaPostOrderOut(BaseModel):
    """Carrier-side view of an order: where it goes and how it is travelling.

    Everything here is the snapshot taken at checkout, so an order stays
    readable without calling the carrier. ``awb_number`` is withheld from the
    guest lookup — it is a tracking key to someone else's parcel.
    """

    model_config = ConfigDict(from_attributes=True)

    settlement_name: str = ""
    division_number: str = ""
    division_address: str = ""
    address_parts: dict | None = None
    awb_number: str | None = None
    status_code: str = ""
    status_text: str = ""


class OrderLookupIn(BaseModel):
    """Body of ``POST /orders/{number}/lookup`` — guest order lookup by email.

    The email travels in the request body, never the URL, so it is not captured
    in access logs or browser history (LP195/2024 — no personal data in URLs).
    """

    email: str = Field(
        ..., min_length=3, description="Contact email of the guest order."
    )


class OrderItemOut(BaseModel):
    """One persisted order line with its name/price snapshot (§2.4)."""

    model_config = ConfigDict(from_attributes=True)

    product_id: int | None
    name_snapshot: str
    price_snapshot: Decimal
    qty: int


class OrderOut(BaseModel):
    """A persisted order with its snapshotted lines (§2.4 / §4)."""

    model_config = ConfigDict(from_attributes=True)

    number: str
    status: str
    payment_status: str
    email: str
    phone: str
    subtotal: Decimal
    discount_total: Decimal
    delivery_cost: Decimal
    total: Decimal
    delivery_type: str
    delivery_service: str = "own"
    delivery_address_id: int | None
    delivery_name: str | None = None
    delivery_city: str | None = None
    delivery_street: str | None = None
    delivery_zip: str | None = None
    payment_method: str
    created_at: datetime
    items: list[OrderItemOut] = Field(default_factory=list)
    # Carrier block; ``None`` for own-logistics orders.
    novapost: NovaPostOrderOut | None = None
    # Set only for a card (maib) checkout: the maib payUrl the storefront
    # redirects the payer to. ``None`` for COD (behaviour unchanged).
    pay_url: str | None = None

    @classmethod
    def from_order(
        cls,
        order: Order,
        items: list[OrderItem],
        novapost: "OrderDeliveryNovaPost | None" = None,
        *,
        hide_awb: bool = False,
    ) -> "OrderOut":
        """Assemble the response DTO from a persisted order and its lines.

        The single canonical projection used by every order read path (customer,
        admin, checkout). Built explicitly rather than via ``from_attributes`` on
        ``Order`` because the ORM ``Order`` intentionally has no ``items``
        relationship. ``pay_url`` stays ``None`` here and is set post-build only
        on the card-checkout path.

        Args:
            order: The persisted order.
            items: The order's lines.
            novapost: The carrier row, when the order ships with Nova Post.
            hide_awb: Withhold the waybill number (guest lookup — anyone who
                knows an order number and email must not get a tracking key).

        Returns:
            OrderOut: The response projection.
        """
        carrier = None
        if novapost is not None:
            carrier = NovaPostOrderOut(
                settlement_name=novapost.settlement_name,
                division_number=novapost.division_number,
                division_address=novapost.division_address,
                address_parts=novapost.address_parts,
                awb_number=None if hide_awb else novapost.awb_number,
                status_code=novapost.status_code,
                status_text=novapost.status_text,
            )
        return cls(
            number=order.number,
            status=order.status,
            payment_status=order.payment_status,
            email=order.email,
            phone=order.phone,
            subtotal=order.subtotal,
            discount_total=order.discount_total,
            delivery_cost=order.delivery_cost,
            total=order.total,
            delivery_type=order.delivery_type,
            delivery_service=order.delivery_service,
            delivery_address_id=order.delivery_address_id,
            delivery_name=order.delivery_name,
            delivery_city=order.delivery_city,
            delivery_street=order.delivery_street,
            delivery_zip=order.delivery_zip,
            payment_method=order.payment_method,
            novapost=carrier,
            created_at=order.created_at,
            items=[OrderItemOut.model_validate(item) for item in items],
        )
