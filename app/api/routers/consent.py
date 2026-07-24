"""Public consent endpoint (LP195/2024, Art.7 — provable consent).

The storefront cookie banner posts each decision here so the store can
demonstrate that consent was given, refused or withdrawn. Public (guests make
most decisions) but rate-limited per IP. Append-only: every call is a new row.
The visitor's ``user_id`` is attached when authenticated (optional cookie/bearer
via :func:`guest_or_user`).
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import guest_or_user
from app.api.ratelimit import rate_limiter
from app.core.config import settings
from app.core.db import get_session
from app.models.consent import CONSENT_POLICY_VERSION
from app.models.user import AppUser
from app.schemas.consent import ConsentAck, ConsentIn
from app.services.consent_service import ConsentService

router = APIRouter(prefix="/consent", tags=["consent"])


@router.post(
    "",
    response_model=ConsentAck,
    dependencies=[Depends(rate_limiter(settings.rate_limit_consent, "consent"))],
)
async def record_consent(
    payload: ConsentIn,
    user: AppUser | None = Depends(guest_or_user),
    session: AsyncSession = Depends(get_session),
) -> ConsentAck:
    """Record one cookie/privacy consent decision (append-only).

    Args:
        payload: The decision (analytics flag + action/source/lang + anon id).
        user: The authenticated user, or ``None`` for a guest.
        session: Injected async DB session.

    Returns:
        ConsentAck: ``{ok, policy_version}`` — the client stores the version.
    """
    await ConsentService(session).record(
        analytics=payload.analytics,
        action=payload.action,
        source=payload.source,
        lang=payload.lang,
        anonymous_id=payload.anonymous_id,
        user_id=user.id if user else None,
    )
    return ConsentAck(policy_version=CONSENT_POLICY_VERSION)
