"""Local test harness for the rate-limit domain (W7).

Builds a minimal FastAPI app mounting only the auth + checkout routers under
``/api/v1``, with the unified exception handlers registered so a raised
``429`` renders as ``{"error": {"code": "rate_limited", ...}}``.

Overrides:

* ``get_session`` -> the shared commit-safe ``db_session`` (auth/checkout
  services commit) from the top-level ``tests/conftest.py``.
* ``get_redis`` -> an isolated client on Redis db 15, flushed before each test
  so windows never leak across tests.
* ``get_auth_service`` -> an :class:`AuthService` bound to the same session +
  isolated redis (mirrors the auth conftest so login exercises the limiter with
  a real, isolated backend).

The ``redis_client`` fixture is also exposed so tests can inspect / expire keys
directly (window-reset assertions) without real waiting.

Run with ``EVIX_TEST_DB=evix_test_ratelimit``.
"""

from collections.abc import AsyncGenerator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_auth_service
from app.api.routers.auth import router as auth_router
from app.api.routers.checkout import router as checkout_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.core.redis import get_redis
from app.services.auth_service import AuthService

_TEST_REDIS_URL = "redis://localhost:56379/15"
_API_V1_PREFIX = "/api/v1"


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[Redis, None]:
    """Yield an isolated Redis client (db 15), flushed before each test."""
    client = Redis.from_url(_TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession,
    redis_client: Redis,
) -> AsyncGenerator[AsyncClient, None]:
    """httpx client on a minimal app with session + redis + auth-service overrides."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(auth_router, prefix=_API_V1_PREFIX)
    app.include_router(checkout_router, prefix=_API_V1_PREFIX)

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _override_redis() -> AsyncGenerator[Redis, None]:
        yield redis_client

    def _override_service() -> AuthService:
        return AuthService(session=db_session, redis=redis_client)

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_redis] = _override_redis
    app.dependency_overrides[get_auth_service] = _override_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.clear()
