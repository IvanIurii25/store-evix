"""Restock test fixtures (Phase 1, §5).

Wires a *local* FastAPI app mounting the customer restock router (and the
admin-catalog router, for the waiter-count endpoint) under ``/api/v1``, with:

* ``get_session`` overridden to the shared commit-safe ``db_session`` (the write
  paths commit; ``db_session`` uses a restarting SAVEPOINT so the outer
  transaction is still rolled back on teardown → full isolation);
* the unified error handlers registered;
* ``current_user`` overridden to a fixed persisted customer (``client``), plus a
  no-override ``guest_client`` to exercise the 401 guard.

Seeding helpers persist a category + product (in/out of stock) and users.

Run with ``EVIX_TEST_DB=evix_test_restock``.
"""

from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff, current_user
from app.api.routers.admin_catalog import router as admin_catalog_router
from app.api.routers.restock import router as restock_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.models.catalog import Category, Product, ProductTranslation
from app.models.user import AppUser

_API_V1_PREFIX = "/api/v1"

# Fixed ids for the overridden authenticated users.
TEST_USER_ID: int = 9001
TEST_STAFF_ID: int = 9002


def _build_app(db_session: AsyncSession) -> FastAPI:
    """Assemble a minimal app mounting the restock + admin-catalog routers."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(restock_router, prefix=_API_V1_PREFIX)
    app.include_router(admin_catalog_router, prefix=_API_V1_PREFIX)

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    return app


@pytest_asyncio.fixture
async def customer(db_session: AsyncSession) -> AppUser:
    """Persist and return a fixed authenticated customer."""
    user = AppUser(
        id=TEST_USER_ID,
        email="waiter@example.com",
        password_hash="x",
        is_active=True,
        is_staff=False,
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest_asyncio.fixture
async def staff_user(db_session: AsyncSession) -> AppUser:
    """Persist and return a fixed authenticated staff user."""
    user = AppUser(
        id=TEST_STAFF_ID,
        email="restock-staff@example.com",
        password_hash="x",
        is_active=True,
        is_staff=True,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def _make_category(db_session: AsyncSession) -> Category:
    """Persist and return a minimal active category."""
    category = Category(path=[], depth=0, is_active=True, position=0)
    db_session.add(category)
    await db_session.flush()
    category.path = [category.id]
    await db_session.flush()
    return category


async def make_product(
    db_session: AsyncSession,
    *,
    code: str,
    qty: int,
    slug: str = "phone",
    with_translations: bool = True,
) -> Product:
    """Persist a product (+ ru/ro translations) with the given stock.

    Args:
        db_session: The test session.
        code: Unique product article code.
        qty: Initial stock (``0`` for out of stock).
        slug: Base slug; the language is suffixed per translation.
        with_translations: When ``False``, skip translations (URL-skip test).

    Returns:
        Product: The persisted product.
    """
    category = await _make_category(db_session)
    product = Product(
        category_id=category.id,
        code=code,
        price=Decimal("100"),
        qty=qty,
        is_active=True,
    )
    db_session.add(product)
    await db_session.flush()
    if with_translations:
        for lang in ("ru", "ro"):
            db_session.add(
                ProductTranslation(
                    product_id=product.id,
                    lang=lang,
                    name=f"Phone {lang}",
                    slug=f"{slug}-{lang}",
                )
            )
        await db_session.flush()
    return product


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession,
    customer: AppUser,
) -> AsyncGenerator[AsyncClient, None]:
    """Customer-path client with ``current_user`` overridden to the customer."""
    app = _build_app(db_session)

    async def _override_user() -> AppUser:
        return customer

    app.dependency_overrides[current_user] = _override_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def staff_client(
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
    """No-auth client: the real ``current_user`` dependency runs (401 guard)."""
    app = _build_app(db_session)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()
