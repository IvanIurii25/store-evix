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

* ``support.fetch_attachment`` — downloads a customer's Telegram photo/document
  (:func:`app.core.telegram.download_file_isolated`, fresh bot), stores it
  privately under a per-conversation key and records that key on the message row
  so the staff proxy can stream it. Enqueued off the webhook ack path.
"""

import asyncio
import mimetypes

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.storage import get_storage, support_attachment_key
from app.core.telegram import download_file_isolated, send_to_chat_isolated
from app.models.support import SupportMessage
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


async def _fetch_attachment(
    session: AsyncSession,
    *,
    message_id: int,
    conversation_id: int,
    file_id: str,
) -> None:
    """Download a Telegram attachment, store it privately and record its key.

    A no-download (empty token dev no-op) short-circuits. The bytes are stored
    under a per-conversation object key (:func:`support_attachment_key`) with a
    content type guessed from the file extension; the resolved key is then set on
    the message row so the staff proxy can stream it.

    Args:
        session: Task-scoped async session.
        message_id: The attachment message's primary key.
        conversation_id: The owning conversation's primary key (key namespace).
        file_id: The Telegram ``file_id`` to download.
    """
    downloaded = await download_file_isolated(file_id)
    if downloaded is None:
        return
    data, ext = downloaded
    content_type = mimetypes.guess_type(f"x{ext}")[0] or "application/octet-stream"
    key = support_attachment_key(conversation_id, ext)
    await get_storage().put_key(key, data, content_type=content_type)
    msg = await session.get(SupportMessage, message_id)
    if msg is not None:
        msg.attachment_key = key
        await session.commit()


@celery_app.task(
    name="support.fetch_attachment",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def fetch_attachment(message_id: int, conversation_id: int, file_id: str) -> None:
    """Download a customer's Telegram attachment and store it privately (S3 key).

    Args:
        message_id: The attachment message's primary key.
        conversation_id: The owning conversation's primary key.
        file_id: The Telegram ``file_id`` to download.
    """
    if not settings.telegram_bot_token:
        return
    run_async_session(
        lambda session: _fetch_attachment(
            session,
            message_id=message_id,
            conversation_id=conversation_id,
            file_id=file_id,
        )
    )
