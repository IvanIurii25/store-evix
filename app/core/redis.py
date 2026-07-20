"""Async Redis client and FastAPI dependency for the refresh-token blacklist (§5.4, §7).

The client is created lazily (not a module-import singleton) so that tests can
override :func:`get_redis` with a client pointed at an isolated Redis db.
"""

from collections.abc import AsyncGenerator

from redis.asyncio import Redis

from app.core.config import settings

_redis_client: Redis | None = None


def _get_client() -> Redis:
    """Return the process-wide async Redis client, creating it on first use.

    Returns:
        Redis: A decode-responses client built from ``settings.redis_url``.
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


async def get_redis() -> AsyncGenerator[Redis, None]:
    """FastAPI dependency yielding the shared async Redis client.

    Yields:
        Redis: The shared client. It is not closed per-request; the connection
            pool is torn down once on shutdown via :func:`close_redis`.
    """
    yield _get_client()


async def close_redis() -> None:
    """Close the shared Redis client and its pool (called on app shutdown)."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
