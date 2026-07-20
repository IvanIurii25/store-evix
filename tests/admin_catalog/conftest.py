"""Admin-catalog test fixtures (B7, §10).

Wires a *local* FastAPI app that mounts only the admin-catalog router under
``/api/v1``, with:

* ``get_session`` overridden to the shared commit-safe ``db_session`` (the write
  paths commit; ``db_session`` uses a restarting SAVEPOINT so the outer
  transaction is still rolled back on teardown → full isolation);
* the unified error handlers registered (so domain errors render the shared
  ``{error:{code,message,details?}}`` envelope);
* ``current_staff`` overridden to a fixed persisted staff user.

Two client flavours are provided: ``client`` (staff, normal admin path) and
``guest_client`` (no override → the real ``current_staff`` runs → the guard is
exercised, 401 without a token).

Run with ``EVIX_TEST_DB=evix_test_admin_catalog``.
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

# Fixed id for the overridden authenticated staff user.
TEST_STAFF_ID: int = 8001


def _build_app(db_session: AsyncSession) -> FastAPI:
    """Assemble a minimal app mounting the admin-catalog router.

    Args:
        db_session: The commit-safe session to bind ``get_session`` to.

    Returns:
        FastAPI: The configured application.
    """
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(admin_catalog_router, prefix=_API_V1_PREFIX)

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    return app


@pytest_asyncio.fixture
async def staff_user(db_session: AsyncSession) -> AppUser:
    """Persist and return a fixed authenticated staff user."""
    user = AppUser(
        id=TEST_STAFF_ID,
        email="admin-catalog@example.com",
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
