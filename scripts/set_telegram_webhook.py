"""Register (or delete) the Telegram webhook for the support bot.

Telegram pushes updates to our public HTTPS endpoint; this one-off script tells
Telegram *where* to push and with *what* secret. Run once per environment after
``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_WEBHOOK_SECRET`` are set:

    uv run python scripts/set_telegram_webhook.py
    uv run python scripts/set_telegram_webhook.py --url https://shop.evix.md/api/v1/telegram/webhook
    uv run python scripts/set_telegram_webhook.py --delete

The default URL is derived from ``STOREFRONT_BASE_URL`` (the API is served on the
same host via the Cloudflare tunnel). Telegram echoes the secret in the
``X-Telegram-Bot-Api-Secret-Token`` header, which the webhook verifies.
"""

import argparse
import asyncio

from aiogram import Bot

from app.core.config import settings

DEFAULT_PATH = "/api/v1/telegram/webhook"


async def apply_webhook(url: str, *, delete: bool) -> str:
    """Set or delete the webhook and return Telegram's resulting state.

    Args:
        url: The public HTTPS endpoint Telegram should push updates to.
        delete: When True, remove the webhook instead of setting it.

    Returns:
        str: A human-readable summary of ``getWebhookInfo`` after the change.
    """
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    if not delete and not settings.telegram_webhook_secret:
        raise SystemExit("TELEGRAM_WEBHOOK_SECRET is not set (webhook must be secured)")

    bot = Bot(token=settings.telegram_bot_token)
    try:
        if delete:
            await bot.delete_webhook(drop_pending_updates=False)
        else:
            await bot.set_webhook(
                url=url,
                secret_token=settings.telegram_webhook_secret,
                drop_pending_updates=False,
            )
        info = await bot.get_webhook_info()
        return f"url={info.url!r} pending={info.pending_update_count}"
    finally:
        await bot.session.close()


def main() -> None:
    """Parse args and apply the webhook change."""
    parser = argparse.ArgumentParser(description="Set/delete the Telegram webhook.")
    parser.add_argument(
        "--url",
        default=f"{settings.storefront_base_url}{DEFAULT_PATH}",
        help="Public webhook URL (default: STOREFRONT_BASE_URL + the webhook path).",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Remove the webhook instead of setting it.",
    )
    args = parser.parse_args()
    result = asyncio.run(apply_webhook(args.url, delete=args.delete))
    action = "deleted" if args.delete else "set"
    print(f"Webhook {action}. {result}")


if __name__ == "__main__":
    main()
