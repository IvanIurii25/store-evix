"""Storage-domain test fixtures (W7).

Provides a minimal FastAPI app mounting only the admin-catalog router (so the
media-upload endpoint is exercisable), with ``get_session`` overridden to the
shared commit-safe ``db_session`` and ``current_staff`` overridden to a fixed
persisted staff user.

Run with ``EVIX_TEST_DB=evix_test_storage``.
"""

from collections.abc import AsyncGenerator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.api.routers.admin_catalog import router as admin_catalog_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.models.user import AppUser

_API_V1_PREFIX = "/api/v1"

# Fixed id for the overridden authenticated staff user (storage domain).
TEST_STAFF_ID: int = 8101


@pytest_asyncio.fixture
async def staff_user(db_session: AsyncSession) -> AppUser:
    """Persist and return a fixed authenticated staff user."""
    user = AppUser(
        id=TEST_STAFF_ID,
        email="storage-admin@example.com",
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
    """Staff client bound to the admin-catalog router over the test session."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(admin_catalog_router, prefix=_API_V1_PREFIX)

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _override_staff() -> AppUser:
        return staff_user

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[current_staff] = _override_staff

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()
