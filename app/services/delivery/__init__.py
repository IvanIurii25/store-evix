"""Delivery carriers: Nova Post client, its development stub, and the factory.

The factory is the only place that decides which backend the rest of the app
talks to, so services and routers stay identical in dev and production.
"""

from redis.asyncio import Redis

from app.core.config import settings
from app.services.delivery.novapost_client import NovaPostClient, NovaPostError
from app.services.delivery.novapost_stub import NovaPostStub

__all__ = [
    "NovaPostClient",
    "NovaPostError",
    "NovaPostStub",
    "new_novapost_client",
]


def new_novapost_client(
    redis: Redis, *, lang: str = "ro"
) -> NovaPostClient | NovaPostStub:
    """Return the carrier backend for the configured mode.

    Args:
        redis: Client used for the shared bearer token (ignored by the stub).
        lang: ``Accept-language`` for localized settlement / branch names.

    Returns:
        NovaPostClient | NovaPostStub: The stub in ``NOVAPOST_MODE=stub`` (dev
        only — the settings validator refuses it elsewhere), else the real
        HTTP client.
    """
    if settings.novapost_stub:
        return NovaPostStub(redis, lang=lang)
    return NovaPostClient(redis, lang=lang)
