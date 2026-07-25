"""Unit tests for the Telegram send path (``send_message`` / bot lifecycle).

The dev no-op (empty token → ``None``) is covered by the reply integration
tests; here we drive the *configured-token* branch with a fake ``Bot`` (no
network): ``send_message`` builds the singleton bot and returns the sent id,
and ``close_telegram`` tears it down. The module-level ``_bot`` singleton is
reset around each test so state never leaks.
"""

import pytest

from app.core import telegram

pytestmark = pytest.mark.asyncio

# A configured (non-empty) bot token that flips sending off the dev no-op path.
_TOKEN = "123:abc"
_CHAT_ID = 777
_SENT_MESSAGE_ID = 9090


class _FakeSentMessage:
    """Stand-in for aiogram's sent-message object (only ``message_id`` used)."""

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class _FakeSession:
    """Records that the bot session was closed on ``close_telegram``."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeBot:
    """Minimal aiogram ``Bot`` replacement: captures sends, no network."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.session = _FakeSession()
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> _FakeSentMessage:
        self.sent.append((chat_id, text))
        return _FakeSentMessage(_SENT_MESSAGE_ID)


@pytest.fixture(autouse=True)
def _reset_bot_singleton(monkeypatch: pytest.MonkeyPatch):
    """Swap in the fake ``Bot`` + a real token and reset the ``_bot`` singleton."""
    monkeypatch.setattr(telegram, "Bot", _FakeBot)
    monkeypatch.setattr(telegram.settings, "telegram_bot_token", _TOKEN)
    monkeypatch.setattr(telegram, "_bot", None)
    yield
    telegram._bot = None


async def test_send_message_builds_bot_and_returns_id() -> None:
    """With a token configured, send_message builds the bot and returns the id."""
    # Act
    result = await telegram.send_message(_CHAT_ID, "hi")

    # Assert
    assert result == _SENT_MESSAGE_ID, "send_message must return the sent message id"
    assert telegram._bot is not None, "the bot singleton must be created on first send"
    assert telegram._bot.sent == [(_CHAT_ID, "hi")], "the send must reach the bot"


async def test_get_bot_is_singleton() -> None:
    """Repeated sends reuse the one lazily-built bot (singleton)."""
    # Act
    await telegram.send_message(_CHAT_ID, "one")
    first = telegram._bot
    await telegram.send_message(_CHAT_ID, "two")

    # Assert
    assert telegram._bot is first, "the bot must be built once and reused"
    assert len(first.sent) == 2, "both sends must go through the same bot"


async def test_close_telegram_closes_and_clears() -> None:
    """close_telegram closes the live bot's session and resets the singleton."""
    # Arrange — force the bot into existence.
    await telegram.send_message(_CHAT_ID, "hi")
    bot = telegram._bot
    assert bot is not None, "arrange: a bot must exist before closing"

    # Act
    await telegram.close_telegram()

    # Assert
    assert bot.session.closed is True, "the bot session must be closed"
    assert telegram._bot is None, "the singleton must be cleared after close"


async def test_close_telegram_noop_when_no_bot() -> None:
    """close_telegram is a safe no-op when no bot was ever created."""
    # Arrange — ensure no bot exists.
    telegram._bot = None

    # Act / Assert — must not raise.
    await telegram.close_telegram()
    assert telegram._bot is None, "no bot to close → still None"
