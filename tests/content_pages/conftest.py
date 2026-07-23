"""Content-page test fixtures (CMS-lite, Phase 1).

Wires a local FastAPI app that mounts the public ``site`` router (for the public
``/site/pages*`` endpoints) plus the admin ``content-pages`` router, with
``get_session`` bound to the commit-safe ``db_session``:

* ``staff_client`` — ``current_staff`` overridden to a fixed persisted staff user;
  drives the admin CRUD and (via the shared session) seeds pages the public
  endpoints then read.
* ``public_client`` — no auth override; exercises the real public endpoints and
  the real ``current_staff`` guard on admin routes.

Run with ``EVIX_TEST_DB=evix_test_content_pages``.
"""

from collections.abc import AsyncGenerator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.api.routers.admin_content_pages import router as admin_content_pages_router
from app.api.routers.site import router as site_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.models.user import AppUser

_API_V1_PREFIX = "/api/v1"

# Fixed id for the overridden authenticated staff user.
TEST_STAFF_ID: int = 8301


def _build_app(db_session: AsyncSession) -> FastAPI:
    """Assemble an app mounting the public site + admin content-pages routers."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(site_router, prefix=_API_V1_PREFIX)
    app.include_router(admin_content_pages_router, prefix=_API_V1_PREFIX)

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    return app


@pytest_asyncio.fixture
async def staff_client(
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    """Staff-path client with ``current_staff`` overridden to a staff user."""
    app = _build_app(db_session)
    staff = AppUser(
        id=TEST_STAFF_ID,
        email="content-pages@example.com",
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


@pytest_asyncio.fixture
async def public_client(
    db_session: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    """No-auth client: public endpoints run; the real admin guard is exercised."""
    app = _build_app(db_session)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()
