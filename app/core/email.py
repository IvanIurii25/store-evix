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

# Bilingual restock-notification copy, keyed by subscription language (§7). The
# Romanian text is the fallback for any unexpected language value.
_RESTOCK_SUBJECT: dict[str, str] = {
    "ru": "Товар снова в наличии — evix",
    "ro": "Produsul este din nou disponibil — evix",
}
_RESTOCK_BODY: dict[str, str] = {
    "ru": "{name} снова в наличии. Успейте заказать:\n{url}\n",
    "ro": "{name} este din nou disponibil. Comandă acum:\n{url}\n",
}
# Language used when a subscription's ``lang`` is missing from the copy tables.
_RESTOCK_DEFAULT_LANG: str = "ro"


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


def _render_restock(
    *,
    to: str,
    product_name: str,
    product_url: str,
    lang: str,
) -> EmailMessage:
    """Build the restock-notification :class:`EmailMessage` (bilingual, §7).

    Args:
        to: Recipient account email.
        product_name: Product display name in the subscription language.
        product_url: Absolute storefront URL of the product card.
        lang: Subscription language (``ru`` | ``ro``); unknown values fall back
            to :data:`_RESTOCK_DEFAULT_LANG`.

    Returns:
        EmailMessage: A ready-to-send message with subject, sender and body.
    """
    copy_lang = lang if lang in _RESTOCK_SUBJECT else _RESTOCK_DEFAULT_LANG
    message = EmailMessage()
    message["From"] = settings.email_from
    message["To"] = to
    message["Subject"] = _RESTOCK_SUBJECT[copy_lang]
    message.set_content(
        _RESTOCK_BODY[copy_lang].format(name=product_name, url=product_url)
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
    await _dispatch(message)


async def _dispatch(message: EmailMessage) -> None:
    """Send a rendered message through the backend from ``settings.email_backend``.

    An unknown backend value warns and falls back to the console backend so a
    misconfiguration never raises inside a post-commit side effect.

    Args:
        message: The rendered message to deliver.

    Raises:
        aiosmtplib.SMTPException: If the SMTP backend fails to deliver.
    """
    if settings.email_backend == BACKEND_SMTP:
        await _send_smtp(message)
        return
    if settings.email_backend != BACKEND_CONSOLE:
        logger.warning(
            "unknown email_backend %r; falling back to console",
            settings.email_backend,
        )
    await _send_console(message)


async def send_restock_notification(
    *,
    to: str,
    product_name: str,
    product_url: str,
    lang: str,
) -> None:
    """Render and dispatch a "back in stock" email (bilingual, §7).

    The backend is selected from ``settings.email_backend`` (console logs, smtp
    delivers) — identical gating to :func:`send_order_confirmation`.

    Args:
        to: Recipient account email.
        product_name: Product display name in the subscription language.
        product_url: Absolute storefront URL of the product card.
        lang: Subscription language (``ru`` | ``ro``).

    Raises:
        aiosmtplib.SMTPException: If the SMTP backend fails to deliver. Callers
            firing this from the notification task must catch it per-recipient so
            one failure never blocks the rest of the batch (§8.6).
    """
    message = _render_restock(
        to=to,
        product_name=product_name,
        product_url=product_url,
        lang=lang,
    )
    await _dispatch(message)
