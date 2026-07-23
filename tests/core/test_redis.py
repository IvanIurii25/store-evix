"""Unit tests for the lazy Redis singleton in :mod:`app.core.redis`.

Covers the ``get_redis`` dependency (yields the shared client), the lazy
singleton reuse, and ``close_redis`` (both the "client present" teardown and the
"nothing to close" no-op). The root ``conftest`` autouse fixture resets the
module-level ``_redis_client`` after each test, keeping usage loop-local.
"""

from __future__ import annotations

from redis.asyncio import Redis

import app.core.redis as redis_module
from app.core.redis import close_redis, get_redis


class TestRedisGetClient:
    """``get_redis`` yields a client and reuses the singleton."""

    async def test_get_redis_yields_redis_client(self):
        # Arrange: ensure a clean singleton slot.
        redis_module._redis_client = None

        # Act: drain the async-generator dependency for its single yield.
        clients = [client async for client in get_redis()]

        # Assert: exactly one live Redis client is yielded.
        assert len(clients) == 1, "get_redis must yield exactly one client"
        assert isinstance(clients[0], Redis), "the yielded value must be a Redis client"

    async def test_get_redis_reuses_singleton_instance(self):
        # Arrange: reset so the first access lazily creates the client.
        redis_module._redis_client = None

        # Act: two independent iterations of the dependency.
        first = [c async for c in get_redis()][0]
        second = [c async for c in get_redis()][0]

        # Assert: both share the one lazily-created singleton.
        assert first is second, "get_redis must reuse the module singleton"


class TestRedisCloseClient:
    """``close_redis`` — teardown of a live client and the no-op path."""

    async def test_close_redis_disposes_existing_client(self):
        # Arrange: force the singleton into existence.
        redis_module._redis_client = None
        _ = [c async for c in get_redis()][0]
        assert redis_module._redis_client is not None, "singleton must exist first"

        # Act: close the shared client.
        await close_redis()

        # Assert: the module reference is cleared after teardown.
        assert redis_module._redis_client is None, "close must clear the singleton"

    async def test_close_redis_no_client_is_noop(self):
        # Arrange: no client present.
        redis_module._redis_client = None

        # Act: closing when nothing exists must not raise.
        await close_redis()

        # Assert: the reference stays cleared.
        assert redis_module._redis_client is None, (
            "close with no client must be a no-op"
        )
