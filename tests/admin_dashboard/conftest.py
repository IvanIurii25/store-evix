"""Admin dashboard + analytics + tracking test fixtures (§6.3).

Mounts the admin-dashboard router and the public track router under ``/api/v1``.
``get_session`` is bound to the commit-safe ``db_session``; ``get_redis`` to an
isolated Redis db (flushed per request) so the track rate-limiter never trips and
never touches the process-wide client. ``current_staff`` is overridden to a fixed
staff user for the admin routes; ``guest_client`` leaves it real for the guard.

Run with ``EVIX_TEST_DB=evix_test_admin_dashboard``.
"""

from collections.abc import AsyncGenerator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.api.routers.admin_dashboard import router as admin_dashboard_router
from app.api.routers.track import router as track_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.core.redis import get_redis
from app.models.user import AppUser

_API_V1_PREFIX = "/api/v1"

TEST_STAFF_ID: int = 8301


def _build_app(db_session: AsyncSession) -> FastAPI:
    """Assemble a minimal app mounting the dashboard + track routers."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(admin_dashboard_router, prefix=_API_V1_PREFIX)
    app.include_router(track_router, prefix=_API_V1_PREFIX)

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _override_redis() -> AsyncGenerator[Redis, None]:
        client = Redis.from_url("redis://localhost:56379/14", decode_responses=True)
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
        email="admin-dashboard@example.com",
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
