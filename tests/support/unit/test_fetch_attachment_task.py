"""Unit tests for the ``support.fetch_attachment`` Celery task body (empty-token).

The sync task wrapper (:func:`app.tasks.support.fetch_attachment`) is a no-op in
dev: with no bot token there is nothing to download, so it must short-circuit
*before* spinning up a fresh session/event loop (``run_async_session``). Called
directly (no worker) with ``run_async_session`` spied so any accidental work
would be caught.
"""

from unittest.mock import Mock

import pytest

import app.tasks.support as support_task

# Fetch payload fed to the task (values are irrelevant on the no-op path).
_MESSAGE_ID = 5
_CONVERSATION_ID = 3
_FILE_ID = "AgACAgIAAxk-file"


def test_noop_when_token_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty bot token → the task returns without opening a session/loop."""
    # Arrange: no bot token, and a spy on the async-session runner that must not
    # run (the guard has to short-circuit before it).
    monkeypatch.setattr(support_task.settings, "telegram_bot_token", "")
    runner = Mock()
    monkeypatch.setattr(support_task, "run_async_session", runner)

    # Act
    support_task.fetch_attachment(_MESSAGE_ID, _CONVERSATION_ID, _FILE_ID)

    # Assert: the dev no-op never touches storage or the DB.
    runner.assert_not_called()


def test_configured_token_delegates_to_run_async_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured token → the task delegates the fetch to run_async_session."""
    # Arrange: a bot token and a spy on the async-session runner.
    monkeypatch.setattr(support_task.settings, "telegram_bot_token", "123:abc")
    runner = Mock()
    monkeypatch.setattr(support_task, "run_async_session", runner)

    # Act
    support_task.fetch_attachment(_MESSAGE_ID, _CONVERSATION_ID, _FILE_ID)

    # Assert: the task hands a coroutine-factory to run_async_session (the fresh
    # session/loop the worker owns), not run inline.
    runner.assert_called_once()
