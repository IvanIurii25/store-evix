"""Pydantic v2 schemas for the cart domain (stage B5, §2.3 / §4).

Request bodies: :class:`AddItem` and :class:`UpdateItem`. Responses: a single
line (:class:`CartItemOut`) and the whole cart (:class:`CartOut`). Money is never
stored on the cart — ``price``, ``line_total`` and the cart ``subtotal`` are
computed on the fly from ``product.price`` at render time (§2.3), so these are
response-only projections, not persisted state.

These are narrow DTOs (only the fields the storefront needs), not god-DTOs.
"""

from decimal import Decimal

from pydantic import BaseModel, Field


class AddItem(BaseModel):
    """Request body for ``POST /cart/items`` — add or increment a product."""

    product_id: int = Field(..., gt=0, description="Catalog product id to add.")
    # Required for variable products (the chosen variant); omit for simple ones.
    variant_id: int | None = Field(
        default=None, gt=0, description="Chosen variant id (variable products)."
    )
    qty: int = Field(..., gt=0, description="Quantity to add (increments if present).")


class UpdateItem(BaseModel):
    """Request body for ``PATCH /cart/items/{product_id}`` — set an absolute qty."""

    qty: int = Field(..., gt=0, description="New absolute quantity for the line.")


class CartItemOut(BaseModel):
    """One cart line with its live price snapshot and computed line total (§2.3)."""

    product_id: int
    # Set for a variable-product line (the chosen variant); NULL for simple lines.
    variant_id: int | None = None
    name: str
    # Localized chosen options, e.g. "Bej, 40×60" — for display next to the name.
    variant_label: str | None = None
    price: Decimal
    qty: int
    line_total: Decimal


class CartOut(BaseModel):
    """The whole cart: live-priced lines plus computed totals (§2.3)."""

    items: list[CartItemOut] = Field(default_factory=list)
    subtotal: Decimal
    item_count: int
