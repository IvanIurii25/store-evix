"""Pydantic v2 schemas for the admin promo-code API (feature A1, §2.5).

Back-office CRUD DTOs for :class:`~app.models.promo.PromoCode`:

* :class:`PromoCreate` — full create body (code + discount + validity window).
* :class:`PromoUpdate` — partial update (every field optional; unset = unchanged).
* :class:`PromoOut` — the persisted row as returned to staff.
* :class:`PromoList` — the roster envelope.

Money / discount values are ``Decimal`` (mapped from ``numeric(12,2)`` — MDL,
§2.6). ``discount_type`` is constrained to the model's allowed set.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.promo import DISCOUNT_TYPES

# Rendered literal set for the ``discount_type`` field description.
_DISCOUNT_CHOICES: str = " | ".join(DISCOUNT_TYPES)


class PromoCreate(BaseModel):
    """Create body for ``POST /admin/promo`` — a full coupon definition."""

    code: str = Field(min_length=1, max_length=64, description="Unique coupon code.")
    discount_type: str = Field(
        ...,
        description=f"Discount kind ({_DISCOUNT_CHOICES}).",
    )
    discount_value: Decimal = Field(
        ...,
        gt=0,
        description="Percent (0<value<=100 for 'percent') or fixed MDL amount.",
    )
    active_from: datetime = Field(..., description="Validity window start (UTC).")
    active_to: datetime = Field(..., description="Validity window end (UTC).")
    min_order_total: Decimal | None = Field(
        default=None,
        ge=0,
        description="Minimum subtotal required to redeem, or null.",
    )
    usage_limit: int | None = Field(
        default=None,
        gt=0,
        description="Maximum number of redemptions, or null for unlimited.",
    )
    is_active: bool = Field(default=True, description="Whether the coupon is enabled.")


class PromoUpdate(BaseModel):
    """Partial update for ``PATCH /admin/promo/{id}`` (unset fields untouched)."""

    code: str | None = Field(default=None, min_length=1, max_length=64)
    discount_type: str | None = Field(
        default=None,
        description=f"Discount kind ({_DISCOUNT_CHOICES}).",
    )
    discount_value: Decimal | None = Field(default=None, gt=0)
    active_from: datetime | None = None
    active_to: datetime | None = None
    min_order_total: Decimal | None = Field(default=None, ge=0)
    usage_limit: int | None = Field(default=None, gt=0)
    is_active: bool | None = None


class PromoOut(BaseModel):
    """A persisted promo code as returned to staff."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    discount_type: str
    discount_value: Decimal
    active_from: datetime
    active_to: datetime
    min_order_total: Decimal | None = None
    usage_limit: int | None = None
    is_active: bool


class PromoList(BaseModel):
    """Envelope for the promo-code roster."""

    data: list[PromoOut] = Field(default_factory=list)
