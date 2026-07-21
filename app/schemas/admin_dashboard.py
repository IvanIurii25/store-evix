"""Pydantic v2 schemas for the admin dashboard + traffic analytics (§6.3).

Response projections for the two overview screens: business health (revenue,
orders, status split, low stock, best sellers) and first-party traffic
(pageviews / unique visitors, per-day series, top paths / referrers, devices).
``NameCount`` is a shared ``{name, count}`` row reused for the several
label→count breakdowns so the front renders them with one component.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class NameCount(BaseModel):
    """A generic ``{name, count}`` breakdown row (status, path, device, ...)."""

    name: str
    count: int


# --------------------------------------------------------------------------- #
# Business dashboard
# --------------------------------------------------------------------------- #
class TopProductOut(BaseModel):
    """A best-selling product line on the dashboard."""

    product_id: int | None = None
    name: str
    qty_sold: int
    revenue: Decimal


class LowStockItem(BaseModel):
    """A product in the low-stock queue."""

    id: int
    code: str
    qty: int


class DashboardSummary(BaseModel):
    """Business-health summary for the resolved window (§6.3)."""

    revenue: Decimal
    orders_count: int
    paid_orders_count: int
    avg_order_value: Decimal
    status_distribution: list[NameCount] = Field(default_factory=list)
    low_stock_count: int
    low_stock: list[LowStockItem] = Field(default_factory=list)
    top_products: list[TopProductOut] = Field(default_factory=list)


class RevenuePointOut(BaseModel):
    """One day of the revenue series."""

    day: date
    revenue: Decimal
    orders_count: int


class RevenueSeries(BaseModel):
    """Per-day revenue + orders over the window."""

    data: list[RevenuePointOut] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Traffic analytics
# --------------------------------------------------------------------------- #
class AnalyticsSummary(BaseModel):
    """First-party traffic summary for the resolved window (§6.3)."""

    pageviews: int
    unique_visitors: int
    bot_pageviews: int
    top_paths: list[NameCount] = Field(default_factory=list)
    top_referrers: list[NameCount] = Field(default_factory=list)
    device_breakdown: list[NameCount] = Field(default_factory=list)


class TrafficPointOut(BaseModel):
    """One day of the traffic series."""

    day: date
    pageviews: int
    unique_visitors: int


class TrafficSeries(BaseModel):
    """Per-day pageviews + unique visitors over the window."""

    data: list[TrafficPointOut] = Field(default_factory=list)
