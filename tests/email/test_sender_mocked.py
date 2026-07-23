"""Unit tests for ``app.core.email`` with ``aiosmtplib.send`` mocked.

Cover the SMTP transport branch and the restock-notification renderer without a
running MailHog: ``aiosmtplib.send`` is replaced with an :class:`AsyncMock` and
its call args are asserted. The real-MailHog delivery test lives in
``test_sender.py`` (skipped when MailHog is down).
"""

import logging
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.core import email

pytestmark = pytest.mark.asyncio


def _use_smtp(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Point the sender at the SMTP backend and stub ``aiosmtplib.send``."""
    monkeypatch.setattr(email.settings, "email_backend", email.BACKEND_SMTP)
    monkeypatch.setattr(email.settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(email.settings, "smtp_port", 2525)
    monkeypatch.setattr(email.settings, "smtp_use_tls", True)
    monkeypatch.setattr(email.settings, "smtp_user", "")
    monkeypatch.setattr(email.settings, "smtp_password", "")
    monkeypatch.setattr(email.settings, "email_from", "orders@evix.md")
    send = AsyncMock()
    monkeypatch.setattr(email.aiosmtplib, "send", send)
    return send


async def test_order_confirmation_smtp_calls_aiosmtplib_send(monkeypatch):
    """SMTP backend dispatches the confirmation via aiosmtplib with settings."""
    send = _use_smtp(monkeypatch)

    await email.send_order_confirmation(
        to="buyer@example.com", order_number="EVX-9001", total=Decimal("10.00")
    )

    send.assert_awaited_once()
    message = send.await_args.args[0]
    assert message["To"] == "buyer@example.com"
    assert message["Subject"] == "Order EVX-9001 confirmed"
    assert send.await_args.kwargs["hostname"] == "smtp.test"
    assert send.await_args.kwargs["port"] == 2525
    assert send.await_args.kwargs["start_tls"] is True
    # Empty credential strings are normalised to None (no auth attempted).
    assert send.await_args.kwargs["username"] is None
    assert send.await_args.kwargs["password"] is None


async def test_smtp_passes_credentials_when_configured(monkeypatch):
    """Configured SMTP user/password are forwarded to aiosmtplib."""
    send = _use_smtp(monkeypatch)
    monkeypatch.setattr(email.settings, "smtp_user", "mailer")
    monkeypatch.setattr(email.settings, "smtp_password", "s3cret")

    await email.send_order_confirmation(
        to="b@example.com", order_number="EVX-9002", total="5.00"
    )

    assert send.await_args.kwargs["username"] == "mailer"
    assert send.await_args.kwargs["password"] == "s3cret"


async def test_restock_notification_console_renders_language(monkeypatch, caplog):
    """Console restock notification logs a message in the subscription language."""
    monkeypatch.setattr(email.settings, "email_backend", email.BACKEND_CONSOLE)

    with caplog.at_level(logging.INFO, logger="app.core.email"):
        await email.send_restock_notification(
            to="waiter@example.com",
            product_name="Телефон",
            product_url="https://shop.evix.md/ru/p/phone",
            lang="ru",
        )

    assert "email(console)" in caplog.text
    assert "waiter@example.com" in caplog.text


async def test_restock_notification_smtp_dispatches(monkeypatch):
    """SMTP restock notification is delivered via aiosmtplib."""
    send = _use_smtp(monkeypatch)

    await email.send_restock_notification(
        to="waiter@example.com",
        product_name="Phone",
        product_url="https://shop.evix.md/ro/p/phone",
        lang="ro",
    )

    send.assert_awaited_once()
    assert send.await_args.args[0]["To"] == "waiter@example.com"


async def test_restock_unknown_language_falls_back_to_default(monkeypatch):
    """An unknown subscription language renders with the default (ro) copy."""
    send = _use_smtp(monkeypatch)

    await email.send_restock_notification(
        to="waiter@example.com",
        product_name="Phone",
        product_url="https://shop.evix.md/p/phone",
        lang="de",
    )

    subject = send.await_args.args[0]["Subject"]
    assert subject == email._RESTOCK_SUBJECT[email._RESTOCK_DEFAULT_LANG]
