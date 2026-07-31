"""Carrier configuration gates (Nova Post phase P1).

Two things must hold no matter what lands in the environment: a half-configured
carrier stays off rather than failing at a customer's checkout, and the fake
carrier can never run outside development. The second is enforced at startup —
a stub that quoted real customers would be a silent, expensive bug.
"""

import pytest

from app.core.config import Settings, settings


def _settings(**over: object) -> Settings:
    """Build a Settings instance with the given overrides (env ignored)."""
    base = {
        "app_env": "local",
        "database_url": "postgresql+asyncpg://x:y@localhost/z",
        "jwt_secret": "test-secret",
    }
    return Settings(**{**base, **over})


def test_disabled_by_default() -> None:
    """With no configuration the carrier is off and exposes no base URL."""
    config = _settings()

    assert config.novapost_enabled is False
    assert config.novapost_stub is False
    assert config.novapost_base_url == ""


def test_stub_mode_counts_as_enabled() -> None:
    """The stub is 'enabled' — that is what unblocks development."""
    config = _settings(novapost_mode="stub")

    assert config.novapost_stub is True
    assert config.novapost_enabled is True


def test_real_mode_needs_token_and_sender() -> None:
    """A half-filled real config stays off instead of failing mid-checkout."""
    token_only = _settings(novapost_mode="sandbox", novapost_api_token="t")
    complete = _settings(
        novapost_mode="sandbox",
        novapost_api_token="t",
        novapost_sender_division_id="d-1",
    )

    assert token_only.novapost_enabled is False
    assert complete.novapost_enabled is True


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("sandbox", "https://api-stage.novapost.pl/v.1.0"),
        ("live", "https://api.novapost.com/v.1.0"),
        ("stub", ""),
    ],
)
def test_base_url_per_mode(mode: str, expected: str) -> None:
    """Each mode maps to its host; the stub has none (it never leaves us)."""
    assert _settings(novapost_mode=mode).novapost_base_url == expected


def test_categories_are_normalized_and_deduplicated() -> None:
    """Whitespace, case and repeats in the env list are cleaned up."""
    config = _settings(novapost_division_categories=" Branch , postomat,branch , ")

    assert config.novapost_categories == ["branch", "postomat"]


def test_stub_is_refused_outside_dev() -> None:
    """Booting a non-dev environment with the fake carrier fails loudly."""
    with pytest.raises(ValueError, match="stub"):
        _settings(app_env="production", novapost_mode="stub")


def test_stub_is_allowed_in_test_env() -> None:
    """The test environment may use the stub (that is how the suite runs)."""
    assert _settings(app_env="test", novapost_mode="stub").novapost_stub is True


def test_live_settings_object_is_not_stubbed() -> None:
    """The process-wide settings never default to the fake carrier."""
    assert settings.novapost_stub is False or settings.app_env in ("local", "test")
