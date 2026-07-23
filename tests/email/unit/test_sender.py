"""Unit tests for the order-confirmation sender (``app.core.email``).

Covers the console backend (renders + logs, sends nothing) and the SMTP backend
against MailHog (really delivers; verified via the MailHog HTTP API). The SMTP
test skips when MailHog is unavailable.
"""

import logging
from decimal import Decimal

import httpx
import pytest

from app.core import email

pytestmark = pytest.mark.asyncio


async def test_console_backend_logs_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Console backend renders the message, logs it, and does not hit SMTP."""
    monkeypatch.setattr(email.settings, "email_backend", email.BACKEND_CONSOLE)

    def _boom(*_args, **_kwargs):
        raise AssertionError("SMTP must not be used by the console backend")

    monkeypatch.setattr(email.aiosmtplib, "send", _boom)

    with caplog.at_level(logging.INFO, logger="app.core.email"):
        await email.send_order_confirmation(
            to="buyer@example.com",
            order_number="EVX-1001",
            total=Decimal("199.00"),
        )

    logged = caplog.text
    assert "email(console)" in logged
    assert "buyer@example.com" in logged
    assert "EVX-1001" in logged


async def test_unknown_backend_falls_back_to_console(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unknown backend value warns and uses the console path (never raises)."""
    monkeypatch.setattr(email.settings, "email_backend", "carrier-pigeon")

    with caplog.at_level(logging.INFO, logger="app.core.email"):
        await email.send_order_confirmation(
            to="buyer@example.com",
            order_number="EVX-1002",
            total=Decimal("50.00"),
        )

    assert "unknown email_backend" in caplog.text
    assert "email(console)" in caplog.text


async def test_smtp_backend_delivers_to_mailhog(
    monkeypatch: pytest.MonkeyPatch,
    mailhog: str,
) -> None:
    """SMTP backend really sends; the message appears in MailHog with right to/subject."""
    monkeypatch.setattr(email.settings, "email_backend", email.BACKEND_SMTP)
    monkeypatch.setattr(email.settings, "smtp_host", "localhost")
    monkeypatch.setattr(email.settings, "smtp_port", 51025)
    monkeypatch.setattr(email.settings, "smtp_use_tls", False)
    monkeypatch.setattr(email.settings, "smtp_user", "")
    monkeypatch.setattr(email.settings, "smtp_password", "")
    monkeypatch.setattr(email.settings, "email_from", "orders@evix.md")

    await email.send_order_confirmation(
        to="mailhog-buyer@example.com",
        order_number="EVX-2001",
        total=Decimal("321.00"),
    )

    resp = httpx.get(f"{mailhog}/api/v2/messages", timeout=3.0)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["total"] == 1, payload
    item = payload["items"][0]
    headers = item["Content"]["Headers"]
    assert headers["To"] == ["mailhog-buyer@example.com"]
    assert headers["Subject"] == ["Order EVX-2001 confirmed"]
    assert headers["From"] == ["orders@evix.md"]
    assert "EVX-2001" in item["Content"]["Body"]
    assert "Cash on delivery" in item["Content"]["Body"]
