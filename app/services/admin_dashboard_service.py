"""Back-office dashboard + traffic-analytics services (§6.3).

Two read-only services that assemble the overview screens over a resolved date
window (:func:`~app.core.timeframe.resolve_window`):

* :class:`DashboardService` — business health: revenue + orders, average order
  value, status distribution, low-stock queue and best sellers.
* :class:`AnalyticsService` — first-party traffic: pageviews / unique visitors /
  bots, per-day series, top paths, top referrers and the device breakdown.

Both return raw dataclasses/tuples; the router maps them to response schemas.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeframe import resolve_window
from app.models.catalog import Product
from app.repositories.admin_dashboard_repo import (
    AdminDashboardRepository,
    RevenuePoint,
    RevenueTotals,
    TopProduct,
)
from app.repositories.analytics_repo import (
    AnalyticsRepository,
    TrafficPoint,
    TrafficSummary,
)
from app.services.admin_catalog_service import LOW_STOCK_THRESHOLD

# Fixed list sizes for the dashboard cards (§6.3).
_TOP_PRODUCTS_LIMIT: int = 5
_LOW_STOCK_LIMIT: int = 10
_TOP_PATHS_LIMIT: int = 10
_TOP_REFERRERS_LIMIT: int = 10


@dataclass(frozen=True)
class DashboardSummaryData:
    """Everything the business dashboard renders for a window (§6.3)."""

    revenue: RevenueTotals
    avg_order_value: Decimal
    status_distribution: list[tuple[str, int]]
    low_stock_count: int
    low_stock: list[Product]
    top_products: list[TopProduct]


@dataclass(frozen=True)
class AnalyticsSummaryData:
    """Everything the traffic dashboard renders for a window (§6.3)."""

    traffic: TrafficSummary
    top_paths: list[tuple[str, int]]
    top_referrers: list[tuple[str, int]]
    device_breakdown: list[tuple[str, int]]


class DashboardService:
    """Assemble the business-health dashboard over a date window (§6.3)."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and its repository."""
        self.session = session
        self.repo = AdminDashboardRepository(session)

    async def summary(
        self,
        date_from: date | None,
        date_to: date | None,
    ) -> DashboardSummaryData:
        """Return the full business summary for the resolved window."""
        start, end = resolve_window(date_from, date_to)
        revenue = await self.repo.revenue_totals(start, end)
        avg = (
            revenue.revenue / revenue.paid_orders_count
            if revenue.paid_orders_count
            else Decimal("0")
        )
        return DashboardSummaryData(
            revenue=revenue,
            avg_order_value=avg,
            status_distribution=await self.repo.status_distribution(start, end),
            low_stock_count=await self.repo.low_stock_count(LOW_STOCK_THRESHOLD),
            low_stock=await self.repo.low_stock(LOW_STOCK_THRESHOLD, _LOW_STOCK_LIMIT),
            top_products=await self.repo.top_products(start, end, _TOP_PRODUCTS_LIMIT),
        )

    async def revenue_series(
        self,
        date_from: date | None,
        date_to: date | None,
    ) -> list[RevenuePoint]:
        """Return the per-day revenue + order-count series for the window."""
        start, end = resolve_window(date_from, date_to)
        return await self.repo.revenue_series(start, end)


class AnalyticsService:
    """Assemble the first-party traffic dashboard over a date window (§6.3)."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and its repository."""
        self.session = session
        self.repo = AnalyticsRepository(session)

    async def summary(
        self,
        date_from: date | None,
        date_to: date | None,
    ) -> AnalyticsSummaryData:
        """Return the full traffic summary for the resolved window."""
        start, end = resolve_window(date_from, date_to)
        return AnalyticsSummaryData(
            traffic=await self.repo.summary(start, end),
            top_paths=await self.repo.top_paths(start, end, _TOP_PATHS_LIMIT),
            top_referrers=await self.repo.top_referrers(
                start, end, _TOP_REFERRERS_LIMIT
            ),
            device_breakdown=await self.repo.device_breakdown(start, end),
        )

    async def traffic_series(
        self,
        date_from: date | None,
        date_to: date | None,
    ) -> list[TrafficPoint]:
        """Return the per-day pageviews + unique-visitors series for the window."""
        start, end = resolve_window(date_from, date_to)
        return await self.repo.series(start, end)
