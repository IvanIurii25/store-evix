"""Data-access layer for first-party traffic analytics (§6.3).

Pure SQL/ORM access — no business rules, no HTTP. Writes one :class:`Visit` per
pageview; reads time-bucketed aggregates for the admin traffic dashboard. Human
(visitor) metrics exclude bots (``is_bot = False``); the device breakdown keeps
the ``bot`` bucket so operators can see automated load separately.

All range queries take a half-open ``[start, end)`` datetime window so day
buckets never double-count a boundary row.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics import Visit


@dataclass(frozen=True)
class TrafficSummary:
    """Top-line traffic numbers for a window (§6.3)."""

    pageviews: int
    unique_visitors: int
    bot_pageviews: int


@dataclass(frozen=True)
class TrafficPoint:
    """One day bucket of traffic (§6.3)."""

    day: datetime
    pageviews: int
    unique_visitors: int


class AnalyticsRepository:
    """Write + read queries for the ``visit`` log, bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used by every query method.

        Args:
            session: Active async session (request- or test-scoped).
        """
        self.session = session

    async def add(self, visit: Visit) -> Visit:
        """Persist one pageview row and return it with a generated id."""
        self.session.add(visit)
        await self.session.flush()
        return visit

    async def summary(self, start: datetime, end: datetime) -> TrafficSummary:
        """Return pageviews / unique visitors / bot pageviews for the window.

        Args:
            start: Inclusive window start.
            end: Exclusive window end.

        Returns:
            TrafficSummary: The top-line counts.
        """
        human = (Visit.occurred_at >= start, Visit.occurred_at < end)
        pageviews = (
            await self.session.scalar(
                select(func.count())
                .select_from(Visit)
                .where(*human, Visit.is_bot.is_(False))
            )
        ) or 0
        unique = (
            await self.session.scalar(
                select(func.count(func.distinct(Visit.session_id))).where(
                    *human, Visit.is_bot.is_(False)
                )
            )
        ) or 0
        bots = (
            await self.session.scalar(
                select(func.count())
                .select_from(Visit)
                .where(*human, Visit.is_bot.is_(True))
            )
        ) or 0
        return TrafficSummary(
            pageviews=int(pageviews),
            unique_visitors=int(unique),
            bot_pageviews=int(bots),
        )

    async def series(self, start: datetime, end: datetime) -> list[TrafficPoint]:
        """Return per-day pageviews + unique visitors (human traffic), ascending.

        Args:
            start: Inclusive window start.
            end: Exclusive window end.

        Returns:
            list[TrafficPoint]: One point per day that has traffic, oldest first.
        """
        day = func.date_trunc("day", Visit.occurred_at).label("day")
        stmt = (
            select(
                day,
                func.count().label("pageviews"),
                func.count(func.distinct(Visit.session_id)).label("unique_visitors"),
            )
            .where(
                Visit.occurred_at >= start,
                Visit.occurred_at < end,
                Visit.is_bot.is_(False),
            )
            .group_by(day)
            .order_by(day)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            TrafficPoint(
                day=row.day,
                pageviews=int(row.pageviews),
                unique_visitors=int(row.unique_visitors),
            )
            for row in rows
        ]

    async def top_paths(
        self,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[tuple[str, int]]:
        """Return the most-viewed paths ``(path, pageviews)`` (human traffic)."""
        stmt = (
            select(Visit.path, func.count().label("n"))
            .where(
                Visit.occurred_at >= start,
                Visit.occurred_at < end,
                Visit.is_bot.is_(False),
            )
            .group_by(Visit.path)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [(row.path, int(row.n)) for row in (await self.session.execute(stmt))]

    async def top_referrers(
        self,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[tuple[str, int]]:
        """Return the top external referrers ``(referrer, pageviews)``."""
        stmt = (
            select(Visit.referrer, func.count().label("n"))
            .where(
                Visit.occurred_at >= start,
                Visit.occurred_at < end,
                Visit.is_bot.is_(False),
                Visit.referrer.is_not(None),
                Visit.referrer != "",
            )
            .group_by(Visit.referrer)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [
            (row.referrer, int(row.n)) for row in (await self.session.execute(stmt))
        ]

    async def device_breakdown(
        self,
        start: datetime,
        end: datetime,
    ) -> list[tuple[str, int]]:
        """Return ``(device, pageviews)`` across all buckets incl. ``bot``."""
        stmt = (
            select(Visit.device, cast(func.count(), Integer).label("n"))
            .where(Visit.occurred_at >= start, Visit.occurred_at < end)
            .group_by(Visit.device)
            .order_by(func.count().desc())
        )
        return [(row.device, int(row.n)) for row in (await self.session.execute(stmt))]
