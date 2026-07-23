"""Public site-config test fixtures (§6.4).

Mounts the public ``site`` router plus the admin ``settings`` router under
``/api/v1`` on one app: the admin router (with ``current_staff`` overridden) is
used only to *seed* SEO defaults via ``PUT``, while ``public_client`` (no auth
override) exercises the real, public ``GET /site/seo``.

Run with ``EVIX_TEST_DB=evix_test_site``.
"""

from collections.abc import AsyncGenerator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.api.routers.admin_settings import router as admin_settings_router
from app.api.routers.site import router as site_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.models.user import AppUser

_API_V1_PREFIX = "/api/v1"
_TEST_STAFF_ID: int = 8201


def _build_app(db_session: AsyncSession) -> FastAPI:
    """Assemble an app mounting the public site + admin-settings routers."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(site_router, prefix=_API_V1_PREFIX)
    app.include_router(admin_settings_router, prefix=_API_V1_PREFIX)

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    return app


@pytest_asyncio.fixture
async def public_client(
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    """No-auth client: the public ``GET /site/seo`` runs without a token."""
    app = _build_app(db_session)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def staff_client(
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    """Staff-path client (only used to seed SEO defaults via PUT)."""
    app = _build_app(db_session)
    staff = AppUser(
        id=_TEST_STAFF_ID,
        email="site-seo@example.com",
        password_hash="x",
        is_active=True,
        is_staff=True,
    )
    db_session.add(staff)
    await db_session.flush()

    async def _override_staff() -> AppUser:
        return staff

    app.dependency_overrides[current_staff] = _override_staff
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()
