"""Local harness for the cross-cutting core tests (deps / lang / ratelimit / ...).

Adds a single ``redis_client`` fixture on an isolated Redis db (13, flushed
before each test) so the auth-dependency and rate-limiter tests get a real but
non-shared backend — no window leaks and no touching the process-wide prod
client across event loops.

Run with ``EVIX_TEST_DB=evix_test_core``.
"""

from collections.abc import AsyncGenerator

import pytest_asyncio
from redis.asyncio import Redis

_TEST_REDIS_URL = "redis://localhost:56379/13"


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[Redis, None]:
    """Yield an isolated Redis client (db 13), flushed before each test."""
    client = Redis.from_url(_TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()
