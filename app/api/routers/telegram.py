"""Public Telegram webhook endpoint for the support helpdesk (support module).

This is the single ingress for inbound customer messages. It is a public path —
Telegram is the only expected caller — and is authenticated purely by the secret
token Telegram echoes in the ``X-Telegram-Bot-Api-Secret-Token`` header (verified
in constant time); a missing or wrong secret is rejected with 403 so forged
updates never reach the service.

Everything else answers ``200`` with ``{"ok": True}``, even for malformed or
non-text updates: Telegram retries any webhook that does not promptly ack, so a
fast, always-successful reply for updates this MVP doesn't handle is a feature,
not a swallowed error. The inbound path is intentionally light (persist + publish
a live event). Heavy or failable side-work — the staff-group notification — is
offloaded to Celery (``support.notify_staff`` via ``.delay``, off the ack path,
per the fast-200 rule).
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ratelimit import rate_limiter
from app.core.config import settings
from app.core.db import get_session
from app.core.redis import get_redis
from app.core.telegram import parse_inbound, verify_webhook_secret
from app.services.support_service import SupportService
from app.tasks.support import notify_staff

router = APIRouter(prefix="/telegram", tags=["telegram"])


@router.post(
    "/webhook",
    dependencies=[Depends(rate_limiter(settings.rate_limit_telegram, "telegram"))],
)
async def telegram_webhook(
    request: Request,
    x_telegram_secret: str | None = Header(
        default=None,
        alias="X-Telegram-Bot-Api-Secret-Token",
    ),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> dict:
    """Ingest one Telegram webhook update and ack it fast.

    Verifies the secret token, parses the update and — when it's a private text
    message this MVP handles — persists it and publishes a live inbox event via
    :class:`~app.services.support_service.SupportService`. When that message
    starts a fresh unread burst (and a staff chat is configured), enqueues the
    ``support.notify_staff`` Celery task so the heavy/failable staff-group send
    happens off the ack path. Any non-handled or malformed update is ignored and
    still answered ``200`` so Telegram stops retrying.

    Args:
        request: The incoming webhook request (raw update in the JSON body).
        x_telegram_secret: The ``X-Telegram-Bot-Api-Secret-Token`` header echoed
            by Telegram; the sole authenticator for this public path.
        session: Injected async DB session.
        redis: Injected async Redis client (live-event publishing).

    Returns:
        dict: ``{"ok": True}`` — a prompt success ack for Telegram.

    Raises:
        HTTPException: 403 if the secret token is missing or does not match.
    """
    if not verify_webhook_secret(x_telegram_secret):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden", "message": "Invalid webhook secret"},
        )

    try:
        update = await request.json()
    except Exception:  # noqa: BLE001 — malformed body: ack anyway so TG stops retrying
        return {"ok": True}

    inbound = parse_inbound(update)
    if inbound is None:
        return {"ok": True}

    conversation_id = await SupportService(session, redis).handle_inbound(inbound)
    if conversation_id is not None and settings.telegram_staff_chat_id:
        name = inbound.customer_name or inbound.customer_username or "клиент"
        notify_staff.delay(conversation_id, name, inbound.text[:200])
    return {"ok": True}
