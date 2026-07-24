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
    """A parsed inbound private text message from a Telegram update."""

    chat_id: int
    message_id: int
    text: str
    customer_name: str | None
    customer_username: str | None
    lang: str | None


def parse_inbound(update: dict) -> InboundMessage | None:
    """Extract a private text message from a raw Telegram update dict.

    Only ``update["message"]`` with non-empty ``"text"`` is handled (MVP is
    text-only): edited messages, callbacks, photos, join events, etc. are
    ignored. Fully defensive — a malformed update yields ``None``, never raises.

    Args:
        update: The raw Telegram update payload (webhook body).

    Returns:
        InboundMessage | None: The parsed message, or ``None`` to ignore.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    if not text:
        return None

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if chat_id is None or message_id is None:
        return None

    sender = message.get("from") or {}
    name = " ".join(
        part for part in (sender.get("first_name"), sender.get("last_name")) if part
    ).strip()

    return InboundMessage(
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        customer_name=name or None,
        customer_username=sender.get("username"),
        lang=sender.get("language_code"),
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
