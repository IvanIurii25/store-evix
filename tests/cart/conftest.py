"""Cart-domain test fixtures (B5).

Provides:

* ``client`` — a local FastAPI app mounting *only* the cart router under
  ``/api/v1``, with ``get_session`` overridden to the shared commit-safe
  ``db_session`` (the cart service commits) and the unified error handlers
  registered. Guests travel by the ``session_token`` cookie.
* ``user_client`` — the same app but with ``guest_or_user`` overridden to a
  fixed, persisted test user, exercising the authenticated cart path.
* ``add_product`` — helper inserting an active product + ``ro`` name so a line
  renders with a name.

``db_session`` comes from the top-level ``tests/conftest.py`` and uses a
restarting SAVEPOINT, so service ``commit()`` calls stay isolated per test.
"""

from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, guest_or_user
from app.api.routers.cart import router as cart_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.models.catalog import Category, Product, ProductTranslation
from app.models.user import AppUser

_API_V1_PREFIX = "/api/v1"

# Fixed id for the overridden authenticated test user.
TEST_USER_ID: int = 9001


def _build_app(db_session: AsyncSession) -> FastAPI:
    """Assemble a minimal app mounting only the cart router (session overridden)."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(cart_router, prefix=_API_V1_PREFIX)

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    return app


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Guest-path client (no auth override) bound to the commit-safe session."""
    app = _build_app(db_session)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def test_user(db_session: AsyncSession) -> AppUser:
    """Persist and return a fixed authenticated test user."""
    user = AppUser(
        id=TEST_USER_ID,
        email="cart-user@example.com",
        password_hash="x",
        is_active=True,
        is_staff=False,
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest_asyncio.fixture
async def user_client(
    db_session: AsyncSession,
    test_user: AppUser,
) -> AsyncGenerator[AsyncClient, None]:
    """Authenticated-path client with auth deps -> the test user.

    Overrides both ``guest_or_user`` (items endpoints) and ``current_user`` (the
    merge endpoint) so the authenticated cart path is exercised without JWTs.
    """
    app = _build_app(db_session)

    async def _override_user() -> AppUser:
        return test_user

    app.dependency_overrides[guest_or_user] = _override_user
    app.dependency_overrides[current_user] = _override_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def add_product(db_session: AsyncSession):
    """Return a helper that inserts an active product + ``ro`` translation.

    Ensures a parent category (id=1) exists first (``product.category_id`` FK).
    """

    async def _ensure_category() -> None:
        if await db_session.get(Category, 1) is None:
            db_session.add(
                Category(id=1, parent_id=None, path=[1], depth=0, is_active=True)
            )
            await db_session.flush()

    async def _add(
        product_id: int,
        *,
        price: Decimal,
        code: str,
        name: str = "Product",
        qty: int = 100,
        is_active: bool = True,
    ) -> Product:
        await _ensure_category()
        product = Product(
            id=product_id,
            category_id=1,
            code=code,
            price=price,
            qty=qty,
            is_active=is_active,
        )
        db_session.add(product)
        db_session.add(
            ProductTranslation(
                product_id=product_id,
                lang="ro",
                name=name,
                slug=f"slug-{code}",
            )
        )
        await db_session.flush()
        return product

    return _add
