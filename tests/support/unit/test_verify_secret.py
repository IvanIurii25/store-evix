"""Unit tests for ``verify_webhook_secret`` (pure, no DB, no network).

The check reads ``settings.telegram_webhook_secret`` at call-time and fails
closed: a missing header or an unconfigured secret is always rejected, even when
a header is supplied.
"""

import pytest

from app.core.config import settings
from app.core.telegram import verify_webhook_secret

# A fixed, non-empty webhook secret used across the verify tests.
_TEST_SECRET: str = "test-secret"


def test_matching_header_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """A header matching the configured secret verifies True."""
    # Arrange
    monkeypatch.setattr(settings, "telegram_webhook_secret", _TEST_SECRET)

    # Act / Assert
    assert verify_webhook_secret(_TEST_SECRET) is True, "exact match → True"


def test_mismatched_header_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """A header differing from the configured secret is rejected."""
    # Arrange
    monkeypatch.setattr(settings, "telegram_webhook_secret", _TEST_SECRET)

    # Act / Assert
    assert verify_webhook_secret("wrong") is False, "mismatch → False"


def test_none_header_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing header is rejected even when a secret is configured."""
    # Arrange
    monkeypatch.setattr(settings, "telegram_webhook_secret", _TEST_SECRET)

    # Act / Assert
    assert verify_webhook_secret(None) is False, "None header → False"


def test_empty_configured_secret_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no secret configured, even a supplied header is rejected."""
    # Arrange
    monkeypatch.setattr(settings, "telegram_webhook_secret", "")

    # Act / Assert
    assert (
        verify_webhook_secret("anything") is False
    ), "empty configured secret must reject all (fail closed)"
