"""Unit tests for ``download_file_isolated`` (throwaway-bot file download).

Mirrors :mod:`test_send_to_chat_isolated`: Celery tasks cannot reuse the shared
module-level bot (its session is bound to the web app's loop), so this helper
builds a *fresh* ``Bot`` per call, resolves + downloads the file and closes the
session in a ``finally``. Two branches:

* empty token (dev) → returns ``None`` without constructing a ``Bot``;
* configured token → resolves via ``get_file``, downloads the bytes and returns
  ``(data, ext)`` with the extension derived from the resolved ``file_path``,
  closing the fresh bot's session afterwards.

Both use a fake ``Bot`` so there is no network; the empty-token case also flags
any accidental construction.
"""

from io import BytesIO
from unittest.mock import AsyncMock

import pytest

from app.core import telegram

pytestmark = pytest.mark.asyncio

# A configured (non-empty) bot token that flips downloading off the dev no-op.
_TOKEN = "123:abc"
# The Telegram ``file_id`` the helper resolves + downloads.
_FILE_ID = "AgACAgIAAxk-file"
# The resolved remote path (its extension drives the returned ``ext``).
_FILE_PATH = "photos/x.jpg"
# The bytes the fake bot streams back for the download.
_DATA = b"\xff\xd8\xff\x00image-bytes"


class _FakeFile:
    """``get_file`` result stub carrying the resolved remote ``file_path``."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path


class _FakeSession:
    """Bot session stub whose ``close`` is an awaited AsyncMock."""

    def __init__(self) -> None:
        self.close = AsyncMock()


class _FakeBot:
    """Fresh-bot stub: fakes ``get_file`` / ``download_file`` and the session."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.session = _FakeSession()
        self.get_file = AsyncMock(return_value=_FakeFile(_FILE_PATH))
        self.download_file = AsyncMock(return_value=BytesIO(_DATA))


class _FlaggingBot:
    """Bot stand-in that fails the test if ever constructed."""

    def __init__(self, token: str) -> None:  # pragma: no cover - must not run
        raise AssertionError("Bot must not be constructed when token is empty")


async def test_empty_token_returns_none_and_builds_no_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty token → returns ``None`` without constructing a ``Bot``."""
    # Arrange: no token, and a Bot that flags any construction attempt.
    monkeypatch.setattr(telegram.settings, "telegram_bot_token", "")
    monkeypatch.setattr(telegram, "Bot", _FlaggingBot)

    # Act
    result = await telegram.download_file_isolated(_FILE_ID)

    # Assert: the dev no-op returns None (no Bot built, no download).
    assert result is None, "empty token → None (dev no-op, no bot)"


async def test_configured_token_downloads_and_closes_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured token → returns ``(data, ext)`` and closes the fresh session."""
    # Arrange: a real token and a fake bot capturing the resolve + download.
    built: list[_FakeBot] = []

    def _factory(token: str) -> _FakeBot:
        bot = _FakeBot(token)
        built.append(bot)
        return bot

    monkeypatch.setattr(telegram.settings, "telegram_bot_token", _TOKEN)
    monkeypatch.setattr(telegram, "Bot", _factory)

    # Act
    result = await telegram.download_file_isolated(_FILE_ID)

    # Assert: the bytes + extension come back, resolved off the file path, and
    # the fresh bot's session is closed (the finally-cleanup).
    assert result == (_DATA, ".jpg"), "returns (bytes, ext derived from file_path)"
    assert len(built) == 1, "a single fresh bot must be built per download"
    bot = built[0]
    bot.get_file.assert_awaited_once_with(_FILE_ID)
    bot.download_file.assert_awaited_once_with(_FILE_PATH)
    bot.session.close.assert_awaited_once()
