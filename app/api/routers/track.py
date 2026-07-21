"""Public pageview-tracking endpoint for first-party analytics (§6.3).

The storefront (Astro middleware) posts one pageview per navigation. This
endpoint is intentionally public (guests generate most traffic) but rate-limited
per IP so it can't be used to flood the visit log. It reads the visitor's
User-Agent from the request header (only to derive a device class — never stored)
and attaches ``user_id`` when the caller is authenticated (optional bearer /
cookie via :func:`guest_or_user`).
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import guest_or_user
from app.api.ratelimit import rate_limiter
from app.core.config import settings
from app.core.db import get_session
from app.models.user import AppUser
from app.schemas.track import PageviewIn, TrackAck
from app.services.track_service import TrackService

router = APIRouter(prefix="/track", tags=["track"])


@router.post(
    "/pageview",
    response_model=TrackAck,
    dependencies=[Depends(rate_limiter(settings.rate_limit_track, "track"))],
)
async def track_pageview(
    payload: PageviewIn,
    request: Request,
    user: AppUser | None = Depends(guest_or_user),
    session: AsyncSession = Depends(get_session),
) -> TrackAck:
    """Record one storefront pageview (§6.3).

    Args:
        payload: The path, session id and optional referrer.
        request: The incoming request (source of the User-Agent).
        user: The authenticated user, or ``None`` for a guest.
        session: Injected async DB session.

    Returns:
        TrackAck: A minimal ``{"ok": true}`` acknowledgement.
    """
    await TrackService(session).record(
        path=payload.path,
        session_id=payload.session_id,
        referrer=payload.referrer,
        user_agent=request.headers.get("User-Agent"),
        user_id=user.id if user else None,
    )
    return TrackAck()
