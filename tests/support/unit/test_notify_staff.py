"""Unit tests for the ``support.notify_staff`` Celery task body.

The task is called directly (no worker): it composes the staff-group ping text
and delegates delivery to :func:`app.core.telegram.send_to_chat_isolated` inside
``asyncio.run``. We monkeypatch that send helper to an ``AsyncMock`` so nothing
touches the network, and assert:

* it is a no-op when either the bot token OR the staff chat id is unset;
* when both are configured, the composed text carries the customer name, the
  snippet and the ``/admin/support`` URL, and is sent to ``telegram_staff_chat_id``.
"""

from unittest.mock import AsyncMock

import pytest

import app.tasks.support as support_task

# A configured bot token and staff group id that enable the send.
_TOKEN = "123:abc"
_STAFF_CHAT_ID = "-100999"
_BASE_URL = "https://shop.evix.md"
# Ping payload fed to the task.
_CONV_ID = 42
_NAME = "Ana"
_SNIPPET = "Salut, am o intrebare"


@pytest.fixture
def _send_spy(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the network send with an AsyncMock and return it for assertions."""
    spy = AsyncMock()
    monkeypatch.setattr(support_task, "send_to_chat_isolated", spy)
    return spy


def test_noop_when_token_empty(
    _send_spy: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty bot token → no staff send is attempted."""
    # Arrange: staff chat configured, but no bot token.
    monkeypatch.setattr(support_task.settings, "telegram_bot_token", "")
    monkeypatch.setattr(support_task.settings, "telegram_staff_chat_id", _STAFF_CHAT_ID)

    # Act
    support_task.notify_staff(_CONV_ID, _NAME, _SNIPPET)

    # Assert
    _send_spy.assert_not_called()


def test_noop_when_staff_chat_empty(
    _send_spy: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty staff chat id → no staff send is attempted."""
    # Arrange: bot token configured, but no staff chat id.
    monkeypatch.setattr(support_task.settings, "telegram_bot_token", _TOKEN)
    monkeypatch.setattr(support_task.settings, "telegram_staff_chat_id", "")

    # Act
    support_task.notify_staff(_CONV_ID, _NAME, _SNIPPET)

    # Assert
    _send_spy.assert_not_called()


def test_sends_composed_text_to_staff_chat(
    _send_spy: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both configured → composed text (name/snippet/admin URL) hits the staff chat."""
    # Arrange: token, staff chat and a known storefront base URL.
    monkeypatch.setattr(support_task.settings, "telegram_bot_token", _TOKEN)
    monkeypatch.setattr(support_task.settings, "telegram_staff_chat_id", _STAFF_CHAT_ID)
    monkeypatch.setattr(support_task.settings, "storefront_base_url", _BASE_URL)

    # Act
    support_task.notify_staff(_CONV_ID, _NAME, _SNIPPET)

    # Assert: one send, to the staff chat, with the expected content.
    _send_spy.assert_awaited_once()
    chat_id, text = _send_spy.await_args.args
    assert chat_id == _STAFF_CHAT_ID, "the ping must target the staff group chat"
    assert _NAME in text, "the composed text must include the customer name"
    assert _SNIPPET in text, "the composed text must include the message snippet"
    assert f"{_BASE_URL}/admin/support" in text, "the text must link to admin support"
