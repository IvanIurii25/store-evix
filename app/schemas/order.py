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

from app.models.order import DELIVERY_TYPES

# Rendered literal set for the ``delivery_type`` field description.
_DELIVERY_CHOICES: str = " | ".join(DELIVERY_TYPES)


class DeliveryAddressIn(BaseModel):
    """Inline courier address (guest or user), snapshotted onto the order so it
    survives independently of any saved address (§2.4 snapshot pattern)."""

    full_name: str = Field(min_length=1, max_length=255)
    city: str = Field(min_length=1, max_length=255)
    street: str = Field(min_length=1, max_length=255)
    zip: str | None = Field(default=None, max_length=32)


class QuoteRequest(BaseModel):
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


class CheckoutRequest(BaseModel):
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


class QuoteOut(BaseModel):
    """Computed checkout totals returned by ``POST /checkout/quote`` (§9)."""

    subtotal: Decimal
    discount_total: Decimal
    delivery_cost: Decimal
    total: Decimal
    delivery_type: str
    item_count: int


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
    delivery_address_id: int | None
    delivery_name: str | None = None
    delivery_city: str | None = None
    delivery_street: str | None = None
    delivery_zip: str | None = None
    payment_method: str
    created_at: datetime
    items: list[OrderItemOut] = Field(default_factory=list)
