"""Promo-domain test fixtures (feature A1, §2.5).

Provides:

* ``client`` — a local FastAPI app mounting only the admin-promo router under
  ``/api/v1`` with ``get_session`` bound to the commit-safe ``db_session`` and
  ``current_staff`` overridden to a fixed persisted staff user.
* ``guest_client`` — no auth override, exercising the real ``current_staff`` guard.
* ``add_promo`` — helper inserting a :class:`PromoCode` for the unit-service tests.

Run with ``EVIX_TEST_DB=evix_test_promo``.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.api.routers.admin_promo import router as admin_promo_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.models.promo import PromoCode
from app.models.user import AppUser

_API_V1_PREFIX = "/api/v1"

# Fixed id for the overridden authenticated staff user.
TEST_STAFF_ID: int = 8301


def _build_app(db_session: AsyncSession) -> FastAPI:
    """Assemble a minimal app mounting the admin-promo router."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(admin_promo_router, prefix=_API_V1_PREFIX)

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    return app


@pytest_asyncio.fixture
async def staff_user(db_session: AsyncSession) -> AppUser:
    """Persist and return a fixed authenticated staff user."""
    user = AppUser(
        id=TEST_STAFF_ID,
        email="admin-promo@example.com",
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
async def add_promo(db_session: AsyncSession):
    """Return a helper inserting a :class:`PromoCode` (currently valid by default)."""

    async def _add(
        code: str,
        *,
        discount_type: str = "percent",
        discount_value: Decimal = Decimal("10"),
        active_from: datetime | None = None,
        active_to: datetime | None = None,
        min_order_total: Decimal | None = None,
        usage_limit: int | None = None,
        is_active: bool = True,
    ) -> PromoCode:
        now = datetime.now(UTC)
        promo = PromoCode(
            code=code,
            discount_type=discount_type,
            discount_value=discount_value,
            active_from=active_from
            if active_from is not None
            else now - timedelta(days=1),
            active_to=active_to if active_to is not None else now + timedelta(days=1),
            min_order_total=min_order_total,
            usage_limit=usage_limit,
            is_active=is_active,
        )
        db_session.add(promo)
        await db_session.flush()
        return promo

    return _add
