"""Pageview ingest service for first-party analytics (§6.3).

Records one :class:`Visit` per storefront pageview. The storefront (Astro
middleware) supplies the path, an optional referrer, its rotating first-party
``session_id`` cookie and the visitor's User-Agent; this service derives the
coarse device class (discarding the raw UA) and optionally attaches ``user_id``
when the visitor is authenticated. Bots are still recorded (flagged) so the
dashboard can separate automated load — the aggregation excludes them from
human metrics.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.user_agent import classify_device
from app.models.analytics import Visit
from app.repositories.analytics_repo import AnalyticsRepository

# Defensive caps so a hostile/broken client can't store unbounded strings.
_PATH_MAX: int = 2048
_REFERRER_MAX: int = 2048
_SESSION_MAX: int = 128


class TrackService:
    """Ingest storefront pageviews into the visit log (§6.3)."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and the analytics repository.

        Args:
            session: Active async session (request- or test-scoped).
        """
        self.session = session
        self.repo = AnalyticsRepository(session)

    async def record(
        self,
        *,
        path: str,
        session_id: str,
        referrer: str | None,
        user_agent: str | None,
        user_id: int | None,
    ) -> Visit:
        """Record one pageview and return the persisted row.

        Args:
            path: The visited storefront path (truncated defensively).
            session_id: The storefront's first-party session cookie value.
            referrer: Optional referrer URL.
            user_agent: The visitor's User-Agent (used only to derive the device
                class; never stored).
            user_id: The authenticated user's id, or ``None`` for a guest.

        Returns:
            Visit: The persisted pageview row.
        """
        device, is_bot = classify_device(user_agent)
        visit = Visit(
            path=path[:_PATH_MAX],
            referrer=referrer[:_REFERRER_MAX] if referrer else None,
            session_id=session_id[:_SESSION_MAX],
            user_id=user_id,
            device=device,
            is_bot=is_bot,
        )
        await self.repo.add(visit)
        await self.session.commit()
        return visit
