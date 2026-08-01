"""Banner test fixtures (homepage carousel, P0).

Mirrors the content-page harness: a local app mounting the public ``site`` router
(for ``GET /site/banners``) plus the admin ``banners`` router, with
``get_session`` bound to the shared ``db_session``.

* ``staff_client`` — ``current_staff`` overridden to a persisted staff user.
* ``public_client`` — no override, so the real guard on admin routes is exercised.
"""

from collections.abc import AsyncGenerator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.api.routers.admin_banners import router as admin_banners_router
from app.api.routers.site import router as site_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.models.user import AppUser

_API_V1_PREFIX = "/api/v1"

# Fixed id for the overridden authenticated staff user.
TEST_STAFF_ID: int = 8401


def _build_app(db_session: AsyncSession) -> FastAPI:
    """Assemble an app mounting the public site + admin banners routers."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(site_router, prefix=_API_V1_PREFIX)
    app.include_router(admin_banners_router, prefix=_API_V1_PREFIX)

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
        email="banners@example.com",
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


def banner_payload(**overrides: object) -> dict:
    """Build a valid create/update payload; overrides replace top-level keys."""
    payload: dict = {
        "position": 0,
        "is_active": True,
        "link_url": "/ru/c/dom",
        "translations": [
            {
                "lang": "ru",
                "image_url": "https://media.evix.md/media/banner-ru.jpg",
                "image_mobile_url": "https://media.evix.md/media/banner-ru-m.jpg",
                "alt": "Летняя распродажа",
            },
            {
                "lang": "ro",
                "image_url": "https://media.evix.md/media/banner-ro.jpg",
                "image_mobile_url": None,
                "alt": "Reduceri de vară",
            },
        ],
    }
    payload.update(overrides)
    return payload
