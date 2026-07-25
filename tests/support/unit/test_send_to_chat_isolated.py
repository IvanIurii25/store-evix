"""Unit tests for ``send_to_chat_isolated`` (throwaway-bot staff send).

Celery tasks cannot reuse the module-level bot singleton (its session is bound
to the web app's loop), so this helper builds a *fresh* ``Bot`` per call and
closes it in a ``finally``. Two branches:

* empty token (dev) → no bot is constructed and no network call is made;
* configured token → ``send_message(chat_id, text)`` is awaited and the fresh
  bot's ``session.close()`` is awaited (the finally-cleanup).

Both branches use a fake ``Bot`` so there is no network; the empty-token case
also flags any accidental construction.
"""

from unittest.mock import AsyncMock

import pytest

from app.core import telegram

pytestmark = pytest.mark.asyncio

# A configured (non-empty) bot token that flips sending off the dev no-op path.
_TOKEN = "123:abc"
# Staff group chat id (string, as the task passes it through).
_STAFF_CHAT_ID = "-100999"
_TEXT = "ping"


class _FakeSession:
    """Bot session stub whose ``close`` is an awaited AsyncMock."""

    def __init__(self) -> None:
        self.close = AsyncMock()


class _FakeBot:
    """Fresh-bot stub: records construction, mocks ``send_message``/session."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.session = _FakeSession()
        self.send_message = AsyncMock()


class _FlaggingBot:
    """Bot stand-in that fails the test if ever constructed."""

    def __init__(self, token: str) -> None:  # pragma: no cover - must not run
        raise AssertionError("Bot must not be constructed when token is empty")


async def test_empty_token_is_noop_and_builds_no_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty token → returns without constructing a Bot or making a call."""
    # Arrange: no token, and a Bot that flags any construction attempt.
    monkeypatch.setattr(telegram.settings, "telegram_bot_token", "")
    monkeypatch.setattr(telegram, "Bot", _FlaggingBot)

    # Act / Assert: the dev no-op must simply return (no Bot, no raise).
    await telegram.send_to_chat_isolated(_STAFF_CHAT_ID, _TEXT)


async def test_configured_token_sends_and_closes_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured token → send_message awaited, then session.close awaited."""
    # Arrange: a real token and a fake bot capturing the send + close.
    built: list[_FakeBot] = []

    def _factory(token: str) -> _FakeBot:
        bot = _FakeBot(token)
        built.append(bot)
        return bot

    monkeypatch.setattr(telegram.settings, "telegram_bot_token", _TOKEN)
    monkeypatch.setattr(telegram, "Bot", _factory)

    # Act: send via the throwaway bot.
    await telegram.send_to_chat_isolated(_STAFF_CHAT_ID, _TEXT)

    # Assert: exactly one fresh bot, the message went out, the session closed.
    assert len(built) == 1, "a single fresh bot must be built per send"
    bot = built[0]
    bot.send_message.assert_awaited_once_with(chat_id=_STAFF_CHAT_ID, text=_TEXT)
    bot.session.close.assert_awaited_once()
