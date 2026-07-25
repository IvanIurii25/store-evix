"""Celery tasks for the Telegram support module (privacy retention).

* ``support.purge_stale`` — scheduled sweep that deletes support conversations
  inactive past the retention window (``settings.support_retention_days``),
  their messages cascading. This is the storage-limitation control (LP195/2024
  Art.5): Telegram support data is not account-linked, so account erasure cannot
  reach it — retention expiry (here) and per-conversation admin delete are the
  two erasure paths.

Uses :func:`app.tasks.base.run_async_session` (a fresh NullPool engine on a
fresh event loop per call — fork-safe) and delegates to
:meth:`SupportService.purge_stale`. The service is built without a Redis client
(``redis=None``): a batch purge publishes no per-row live events.

* ``support.notify_staff`` — durable staff-group ping fired off the webhook ack
  path when an inbound message starts a fresh unread burst. Sends via a fresh
  bot (:func:`app.core.telegram.send_to_chat_isolated`), never the shared
  singleton, because the task runs on its own event loop.
"""

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.telegram import send_to_chat_isolated
from app.services.support_service import SupportService
from app.tasks.base import run_async_session


async def _purge(session: AsyncSession) -> int:
    """Purge support conversations inactive past the retention window.

    Args:
        session: Task-scoped async session.

    Returns:
        int: The number of conversations deleted.
    """
    return await SupportService(session).purge_stale(settings.support_retention_days)


@celery_app.task(name="support.purge_stale")
def purge_stale_conversations() -> int:
    """Delete support conversations inactive past the retention window (LP195 Art.5).

    Returns:
        int: The number of conversations deleted.
    """
    return run_async_session(_purge)


async def _send_staff(text: str) -> None:
    """Post ``text`` to the configured staff Telegram group (fresh bot).

    Args:
        text: The message body to deliver to the staff group.
    """
    await send_to_chat_isolated(settings.telegram_staff_chat_id, text)


@celery_app.task(
    name="support.notify_staff",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def notify_staff(conversation_id: int, name: str, snippet: str) -> None:
    """Post a new-support-message ping to the staff Telegram group (durable, retried).

    Args:
        conversation_id: The conversation the inbound message belongs to.
        name: The customer's display name (or a fallback label).
        snippet: A truncated preview of the inbound message text.
    """
    if not settings.telegram_bot_token or not settings.telegram_staff_chat_id:
        return
    admin_url = f"{settings.storefront_base_url}/admin/support"
    text = f"💬 Новое сообщение в поддержке от {name}:\n{snippet}\n\n{admin_url}"
    asyncio.run(_send_staff(text))
