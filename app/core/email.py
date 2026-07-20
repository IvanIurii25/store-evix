"""Order-confirmation email sender (§9.8).

A small pluggable mail layer used by the checkout post-commit side effect. The
active backend is chosen by ``settings.email_backend``:

* ``console`` — logs the rendered message and sends nothing. Default for local
  dev and tests, so no SMTP server is required to exercise the checkout flow.
* ``smtp`` — delivers via ``aiosmtplib`` to ``smtp_host:smtp_port`` using
  ``email_from`` as the sender, with optional STARTTLS and auth.

The public entry point is :func:`send_order_confirmation`; it renders the
message once and dispatches it through the selected backend. Rendering is kept
separate from transport so both backends share one canonical message body.
"""

import logging
from decimal import Decimal
from email.message import EmailMessage

import aiosmtplib

from app.core.config import settings

logger = logging.getLogger(__name__)

# Backend identifiers accepted in ``settings.email_backend``.
BACKEND_CONSOLE: str = "console"
BACKEND_SMTP: str = "smtp"

# COD (cash-on-delivery) instruction included in every confirmation body (§9.8).
_COD_INSTRUCTION: str = (
    "Payment method: Cash on delivery (COD). Please prepare the exact amount "
    "to hand to the courier on delivery."
)


def _render_confirmation(
    *,
    to: str,
    order_number: str,
    total: Decimal | str,
    currency: str,
) -> EmailMessage:
    """Build the order-confirmation :class:`EmailMessage`.

    Args:
        to: Recipient contact email.
        order_number: The created order number.
        total: The order grand total.
        currency: Currency code shown next to the total.

    Returns:
        EmailMessage: A ready-to-send message with subject, sender and body.
    """
    message = EmailMessage()
    message["From"] = settings.email_from
    message["To"] = to
    message["Subject"] = f"Order {order_number} confirmed"
    message.set_content(
        f"Thank you for your order!\n\n"
        f"Order number: {order_number}\n"
        f"Total: {total} {currency}\n\n"
        f"{_COD_INSTRUCTION}\n"
    )
    return message


async def _send_console(message: EmailMessage) -> None:
    """'Send' a message by logging it — the no-transport dev/test backend.

    Args:
        message: The rendered confirmation message.
    """
    logger.info(
        "email(console) to=%s subject=%r body=%r",
        message["To"],
        message["Subject"],
        message.get_content(),
    )


async def _send_smtp(message: EmailMessage) -> None:
    """Deliver a message over SMTP via ``aiosmtplib`` (§9.8).

    Uses ``settings.smtp_host`` / ``smtp_port`` and optional STARTTLS + auth.

    Args:
        message: The rendered confirmation message.

    Raises:
        aiosmtplib.SMTPException: If the SMTP conversation fails.
    """
    username = settings.smtp_user or None
    password = settings.smtp_password or None
    await aiosmtplib.send(
        message,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        start_tls=settings.smtp_use_tls,
        username=username,
        password=password,
    )
    logger.info(
        "email(smtp) sent to=%s subject=%r",
        message["To"],
        message["Subject"],
    )


async def send_order_confirmation(
    *,
    to: str,
    order_number: str,
    total: Decimal | str,
    currency: str | None = None,
) -> None:
    """Render and dispatch the order-confirmation email (§9.8).

    The backend is selected from ``settings.email_backend``; an unknown value
    falls back to the console backend so a misconfiguration never raises inside
    the post-commit side effect.

    Args:
        to: Recipient contact email.
        order_number: The created order number.
        total: The order grand total.
        currency: Currency code; defaults to ``settings.currency``.

    Raises:
        aiosmtplib.SMTPException: If the SMTP backend fails to deliver. Callers
            firing this post-commit must catch it so a mail failure never
            invalidates a committed order.
    """
    message = _render_confirmation(
        to=to,
        order_number=order_number,
        total=total,
        currency=currency or settings.currency,
    )
    if settings.email_backend == BACKEND_SMTP:
        await _send_smtp(message)
        return
    if settings.email_backend != BACKEND_CONSOLE:
        logger.warning(
            "unknown email_backend %r; falling back to console",
            settings.email_backend,
        )
    await _send_console(message)
