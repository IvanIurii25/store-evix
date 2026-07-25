"""Thin async wrapper over aiogram for the Telegram support helpdesk.

Mirrors :mod:`app.core.redis`: a lazy module-level ``Bot`` singleton (created on
first use, torn down once on shutdown via :func:`close_telegram`) plus a couple
of pure helpers the service layer uses.

The bot token is empty in dev (:attr:`settings.telegram_bot_token`), so sending
is a no-op that returns ``None`` rather than constructing an invalid ``Bot``.
Inbound webhook updates arrive as raw dicts; :func:`parse_inbound` extracts the
one shape this MVP handles — a private text message — and ignores everything
else defensively (never raising on malformed payloads).
"""

import os
import secrets
from dataclasses import dataclass

from aiogram import Bot

from app.core.config import settings

_bot: Bot | None = None


def _get_bot() -> Bot | None:
    """Return the process-wide ``Bot``, creating it on first use.

    Returns:
        Bot | None: The shared bot built from ``settings.telegram_bot_token``,
            or ``None`` if no token is configured (dev), so sending is a no-op.
    """
    global _bot
    if not settings.telegram_bot_token:
        return None
    if _bot is None:
        _bot = Bot(token=settings.telegram_bot_token)
    return _bot


@dataclass(frozen=True)
class InboundMessage:
    """A parsed inbound private message from a Telegram update.

    Carries a plain text body and, when the customer sent a photo or document,
    the attachment reference (``attachment_file_id`` / ``attachment_kind`` /
    ``attachment_name``). For an attachment message ``text`` is the caption (may
    be empty). All three attachment fields are ``None`` for a plain text message.
    """

    chat_id: int
    message_id: int
    text: str
    customer_name: str | None
    customer_username: str | None
    lang: str | None
    attachment_file_id: str | None = None
    attachment_kind: str | None = None
    attachment_name: str | None = None


def _parse_attachment(message: dict) -> tuple[str, str, str | None] | None:
    """Extract an attachment reference (file id, kind, name) from a message.

    Recognizes a photo (``message["photo"]`` — a non-empty list of sizes, of
    which the last is the largest) and a document (``message["document"]``).
    Fully defensive — anything else yields ``None``.

    Args:
        message: The raw Telegram ``message`` dict.

    Returns:
        tuple[str, str, str | None] | None: ``(file_id, kind, name)`` — ``kind``
        is ``"photo"`` (``name`` is ``None``) or ``"document"`` (``name`` is the
        original filename, if any) — or ``None`` for a non-attachment message.
    """
    photo = message.get("photo")
    if isinstance(photo, list) and photo:
        file_id = (photo[-1] or {}).get("file_id")
        if file_id:
            return file_id, "photo", None
    document = message.get("document")
    if isinstance(document, dict):
        file_id = document.get("file_id")
        if file_id:
            return file_id, "document", document.get("file_name")
    return None


def parse_inbound(update: dict) -> InboundMessage | None:
    """Extract a private message from a raw Telegram update dict.

    Handles ``update["message"]`` when it carries either non-empty ``"text"`` or
    a photo/document attachment (the caption becomes ``text``, possibly empty).
    Edited messages, callbacks, join events, stickers, video, etc. are ignored.
    Fully defensive — a malformed update yields ``None``, never raises.

    Args:
        update: The raw Telegram update payload (webhook body).

    Returns:
        InboundMessage | None: The parsed message, or ``None`` to ignore.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return None

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if chat_id is None or message_id is None:
        return None

    attachment = _parse_attachment(message)
    text = message.get("text")
    if attachment is not None:
        file_id, kind, name = attachment
        text = message.get("caption") or ""
    elif text:
        file_id = kind = name = None
    else:
        return None

    sender = message.get("from") or {}
    display_name = " ".join(
        part for part in (sender.get("first_name"), sender.get("last_name")) if part
    ).strip()

    return InboundMessage(
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        customer_name=display_name or None,
        customer_username=sender.get("username"),
        lang=sender.get("language_code"),
        attachment_file_id=file_id,
        attachment_kind=kind,
        attachment_name=name,
    )


async def send_message(chat_id: int, text: str) -> int | None:
    """Send a message to a Telegram chat and return its message id.

    Args:
        chat_id: The target Telegram chat id.
        text: The message body.

    Returns:
        int | None: The sent message's id, or ``None`` if no token is
            configured (dev no-op). aiogram errors propagate to the caller.
    """
    bot = _get_bot()
    if bot is None:
        return None
    sent = await bot.send_message(chat_id=chat_id, text=text)
    return sent.message_id


async def send_to_chat_isolated(chat_id: str, text: str) -> None:
    """Send a message via a throwaway ``Bot``, closing its session after.

    For use from Celery tasks, where the shared module-level bot singleton's
    session is bound to the web app's event loop and cannot be reused across the
    fresh ``asyncio.run`` loop each task spins up. Builds a fresh ``Bot``, sends,
    and closes its session in a ``finally``.

    Args:
        chat_id: The target Telegram chat id (staff group).
        text: The message body.
    """
    if not settings.telegram_bot_token:
        return
    bot = Bot(token=settings.telegram_bot_token)
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    finally:
        await bot.session.close()


async def download_file_isolated(file_id: str) -> tuple[bytes, str] | None:
    """Download a Telegram file's bytes via a throwaway ``Bot``, closing after.

    For use from Celery tasks (mirrors :func:`send_to_chat_isolated`): the shared
    module-level bot's session is bound to the web app's event loop and cannot be
    reused across the fresh ``asyncio.run`` loop each task spins up. Resolves the
    file via ``get_file``, downloads its bytes, and closes the session in a
    ``finally``.

    Args:
        file_id: The Telegram ``file_id`` of the photo/document to download.

    Returns:
        tuple[bytes, str] | None: ``(data, ext)`` where ``ext`` is the file
        extension including the leading dot (e.g. ``.jpg``, or ``""`` when the
        source path has none), or ``None`` when no token is configured (dev
        no-op).
    """
    if not settings.telegram_bot_token:
        return None
    bot = Bot(token=settings.telegram_bot_token)
    try:
        file = await bot.get_file(file_id)
        buffer = await bot.download_file(file.file_path)
        data = buffer.read()
        ext = os.path.splitext(file.file_path or "")[1]
        return data, ext
    finally:
        await bot.session.close()


def verify_webhook_secret(header_value: str | None) -> bool:
    """Constant-time compare of the webhook secret header against the config.

    Fails closed: if no secret is configured, unauthenticated updates are
    rejected (a misconfigured prod must not accept them).

    Args:
        header_value: The ``X-Telegram-Bot-Api-Secret-Token`` header, or ``None``.

    Returns:
        bool: ``True`` only if the header matches a non-empty configured secret.
    """
    if not settings.telegram_webhook_secret or header_value is None:
        return False
    return secrets.compare_digest(header_value, settings.telegram_webhook_secret)


async def close_telegram() -> None:
    """Close the shared bot's session, if created (called on app shutdown)."""
    global _bot
    if _bot is not None:
        await _bot.session.close()
        _bot = None
