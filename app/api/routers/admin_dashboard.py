"""Admin dashboard + traffic-analytics API (§6.3).

Read-only back-office overview behind ``Depends(current_staff)`` (JWT +
``is_staff`` → 403 for non-staff). It delegates to
:class:`~app.services.admin_dashboard_service.DashboardService` (business health)
and :class:`~app.services.admin_dashboard_service.AnalyticsService` (first-party
traffic), and projects the returned dataclasses into response schemas. No
business logic and no SQL live here.

Endpoints (the ``/api/v1`` prefix is added by the integrator when mounting):

* ``GET /admin/dashboard/summary`` — revenue, orders, AOV, status split, low
  stock, best sellers.
* ``GET /admin/dashboard/revenue-series`` — per-day revenue + orders.
* ``GET /admin/analytics/summary`` — pageviews / unique visitors / bots, top
  paths, top referrers, device breakdown.
* ``GET /admin/analytics/traffic-series`` — per-day pageviews + unique visitors.

All accept an optional ``date_from`` / ``date_to`` (calendar dates); the window
defaults to the last 30 days (§6.3).
"""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.core.db import get_session
from app.models.user import AppUser
from app.schemas.admin_dashboard import (
    AnalyticsSummary,
    DashboardSummary,
    LowStockItem,
    NameCount,
    RevenuePointOut,
    RevenueSeries,
    TopProductOut,
    TrafficPointOut,
    TrafficSeries,
)
from app.services.admin_dashboard_service import (
    AnalyticsService,
    AnalyticsSummaryData,
    DashboardService,
    DashboardSummaryData,
)

router = APIRouter(
    prefix="/admin",
    tags=["admin-dashboard"],
    dependencies=[Depends(current_staff)],
)


def _counts(pairs: list[tuple[str, int]]) -> list[NameCount]:
    """Map ``(name, count)`` pairs into ``NameCount`` rows."""
    return [NameCount(name=name, count=count) for name, count in pairs]


def _to_dashboard(data: DashboardSummaryData) -> DashboardSummary:
    """Project the business summary dataclass into its response schema."""
    return DashboardSummary(
        revenue=data.revenue.revenue,
        orders_count=data.revenue.orders_count,
        paid_orders_count=data.revenue.paid_orders_count,
        avg_order_value=data.avg_order_value,
        status_distribution=_counts(data.status_distribution),
        low_stock_count=data.low_stock_count,
        low_stock=[
            LowStockItem(id=p.id, code=p.code, qty=p.qty) for p in data.low_stock
        ],
        top_products=[
            TopProductOut(
                product_id=tp.product_id,
                name=tp.name,
                qty_sold=tp.qty_sold,
                revenue=tp.revenue,
            )
            for tp in data.top_products
        ],
    )


def _to_analytics(data: AnalyticsSummaryData) -> AnalyticsSummary:
    """Project the traffic summary dataclass into its response schema."""
    return AnalyticsSummary(
        pageviews=data.traffic.pageviews,
        unique_visitors=data.traffic.unique_visitors,
        bot_pageviews=data.traffic.bot_pageviews,
        top_paths=_counts(data.top_paths),
        top_referrers=_counts(data.top_referrers),
        device_breakdown=_counts(data.device_breakdown),
    )


@router.get("/dashboard/summary", response_model=DashboardSummary)
async def dashboard_summary(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> DashboardSummary:
    """Return the business-health summary for the window (default last 30d)."""
    data = await DashboardService(session).summary(date_from, date_to)
    return _to_dashboard(data)


@router.get("/dashboard/revenue-series", response_model=RevenueSeries)
async def revenue_series(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> RevenueSeries:
    """Return the per-day revenue + orders series for the window."""
    points = await DashboardService(session).revenue_series(date_from, date_to)
    return RevenueSeries(
        data=[
            RevenuePointOut(
                day=p.day.date(),
                revenue=p.revenue,
                orders_count=p.orders_count,
            )
            for p in points
        ]
    )


@router.get("/analytics/summary", response_model=AnalyticsSummary)
async def analytics_summary(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> AnalyticsSummary:
    """Return the first-party traffic summary for the window."""
    data = await AnalyticsService(session).summary(date_from, date_to)
    return _to_analytics(data)


@router.get("/analytics/traffic-series", response_model=TrafficSeries)
async def traffic_series(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> TrafficSeries:
    """Return the per-day pageviews + unique-visitors series for the window."""
    points = await AnalyticsService(session).traffic_series(date_from, date_to)
    return TrafficSeries(
        data=[
            TrafficPointOut(
                day=p.day.date(),
                pageviews=p.pageviews,
                unique_visitors=p.unique_visitors,
            )
            for p in points
        ]
    )
