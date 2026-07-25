"""Support-module test fixtures (Telegram helpdesk).

Wires a *local* FastAPI app mounting BOTH the public Telegram webhook router and
the admin support router under ``/api/v1``, with:

* ``get_session`` overridden to the shared commit-safe ``db_session`` (the write
  paths commit; ``db_session`` uses a restarting SAVEPOINT so the outer
  transaction is still rolled back on teardown → full isolation);
* ``get_redis`` overridden to an isolated Redis db (15, flushed per request) so
  the service's live-event publishes and the webhook's rate-limiter never touch
  the process-wide prod client across event loops;
* the unified error handlers registered;
* ``current_staff`` overridden to a fixed persisted staff user (``client``),
  plus a no-override ``guest_client`` to exercise the auth guard.

Run with ``EVIX_TEST_DB=evix_test_support``.
"""

from collections.abc import AsyncGenerator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.api.routers.admin_support import router as admin_support_router
from app.api.routers.telegram import router as telegram_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.core.redis import get_redis
from app.models.user import AppUser

_API_V1_PREFIX = "/api/v1"

# Isolated Redis db for support tests: pub/sub events + webhook rate-limiter.
SUPPORT_REDIS_URL: str = "redis://localhost:56379/15"

# Fixed id for the overridden authenticated staff user.
TEST_STAFF_ID: int = 7001


def _build_app(db_session: AsyncSession) -> FastAPI:
    """Assemble a minimal app mounting the telegram + admin-support routers.

    Args:
        db_session: The commit-safe session to bind ``get_session`` to.

    Returns:
        FastAPI: The configured application with ``get_session``/``get_redis``
        overridden to isolated test resources.
    """
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(telegram_router, prefix=_API_V1_PREFIX)
    app.include_router(admin_support_router, prefix=_API_V1_PREFIX)

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _override_redis() -> AsyncGenerator[Redis, None]:
        client = Redis.from_url(SUPPORT_REDIS_URL, decode_responses=True)
        await client.flushdb()
        try:
            yield client
        finally:
            await client.aclose()

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_redis] = _override_redis
    return app


@pytest_asyncio.fixture
async def staff_user(db_session: AsyncSession) -> AppUser:
    """Persist and return a fixed authenticated staff user."""
    user = AppUser(
        id=TEST_STAFF_ID,
        email="support-staff@example.com",
        password_hash="x",
        is_active=True,
        is_staff=True,
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession,
    staff_user: AppUser,
) -> AsyncGenerator[AsyncClient, None]:
    """Staff-path client with ``current_staff`` overridden to the staff user."""
    app = _build_app(db_session)

    async def _override_staff() -> AppUser:
        return staff_user

    app.dependency_overrides[current_staff] = _override_staff
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def guest_client(
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    """No-auth client: the real ``current_staff`` dependency runs (guard test)."""
    app = _build_app(db_session)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[Redis, None]:
    """A standalone isolated Redis client (flushed) for direct pub/sub tests.

    Bound to the same isolated db (15) the ``client`` fixture publishes on, so a
    subscriber started here sees events emitted through the app.
    """
    client = Redis.from_url(SUPPORT_REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()
