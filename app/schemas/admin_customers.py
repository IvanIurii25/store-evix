"""Pydantic v2 schemas for the customers back office (§6.2).

Two views:

* :class:`CustomerListItem` (in the shared :class:`~app.core.pagination.Page`
  envelope) — one roster row with order stats (count / lifetime spend / last
  order) resolved without an N+1.
* :class:`CustomerDetail` — the full profile: contact + flags + loyalty, the
  customer's saved addresses, their order history, and the same aggregate stats.

Narrow DTOs — only what the back office renders; the customer's password hash is
never projected.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.core.pagination import Page


class CustomerListItem(BaseModel):
    """One customer row in the back-office roster (§6.2)."""

    id: int
    email: str
    phone: str | None = None
    created_at: datetime
    orders_count: int
    total_spent: Decimal
    last_order_at: datetime | None = None


# List envelope for ``GET /admin/customers`` (shared pagination envelope, §4).
CustomerList = Page[CustomerListItem]


class CustomerAddress(BaseModel):
    """A saved delivery address shown on the customer detail."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    phone: str
    city: str
    street: str
    zip: str | None = None
    is_default: bool


class CustomerOrderSummary(BaseModel):
    """A compact order row in the customer's history."""

    model_config = ConfigDict(from_attributes=True)

    number: str
    status: str
    payment_status: str
    total: Decimal
    created_at: datetime


class CustomerDetail(BaseModel):
    """Full back-office view of one customer (§6.2)."""

    id: int
    email: str
    phone: str | None = None
    is_active: bool
    created_at: datetime
    loyalty_points: Decimal
    orders_count: int
    total_spent: Decimal
    last_order_at: datetime | None = None
    addresses: list[CustomerAddress] = Field(default_factory=list)
    orders: list[CustomerOrderSummary] = Field(default_factory=list)
