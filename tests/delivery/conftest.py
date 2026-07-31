"""Test harness for the delivery domain (Nova Post phase P1).

Mounts only the delivery router on a minimal app, with the unified exception
handlers registered so a 404/502/429 renders as the usual
``{"error": {"code": …}}`` envelope.

Redis is isolated on db 13 and flushed per test: these endpoints cache lookups
and count rate-limit windows, so a leaked key from a neighbouring test would
make results depend on execution order. No database is involved — the delivery
lookups never touch it.
"""

from collections.abc import AsyncGenerator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from app.api.routers.delivery import router as delivery_router
from app.core.errors import register_exception_handlers
from app.core.redis import get_redis

_TEST_REDIS_URL = "redis://localhost:56379/13"
_API_V1_PREFIX = "/api/v1"


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[Redis, None]:
    """Yield an isolated Redis client (db 13), flushed before each test."""
    client = Redis.from_url(_TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def client(redis_client: Redis) -> AsyncGenerator[AsyncClient, None]:
    """httpx client on a minimal app exposing only the delivery router."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(delivery_router, prefix=_API_V1_PREFIX)

    async def _override_redis() -> AsyncGenerator[Redis, None]:
        yield redis_client

    app.dependency_overrides[get_redis] = _override_redis

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.clear()
