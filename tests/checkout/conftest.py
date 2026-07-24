"""Checkout-domain test fixtures (B6).

Provides:

* ``client`` — a local FastAPI app mounting the cart, checkout and orders routers
  under ``/api/v1``, with ``get_session`` overridden to the shared commit-safe
  ``db_session`` (checkout / cart services commit) and the unified error handlers
  registered. Guests travel by the ``session_token`` cookie set by the cart
  router. No auth override → the guest checkout path is exercised.
* ``user_client`` — the same app but with ``guest_or_user`` / ``current_user``
  overridden to a fixed, persisted test user, exercising the authenticated path.
* ``add_product`` — helper inserting an active product (+ ``ro`` name) with a
  chosen stock ``qty`` so a cart line renders and stock rules can be tested.

``db_session`` comes from the top-level ``tests/conftest.py`` and uses a
restarting SAVEPOINT, so service ``commit()`` calls stay isolated per test.

Run with ``EVIX_TEST_DB=evix_test_checkout``.
"""

from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, guest_or_user
from app.api.routers.cart import router as cart_router
from app.api.routers.checkout import router as checkout_router
from app.api.routers.orders import router as orders_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.core.redis import get_redis
from app.models.catalog import Category, Product, ProductTranslation
from app.models.user import Address, AppUser

_API_V1_PREFIX = "/api/v1"

# Isolated Redis db for the rate limiter (checkout is rate-limited). db 14 keeps
# it clear of auth's db 15; flushed per request so these tests never hit a limit.
_TEST_REDIS_URL = "redis://localhost:56379/14"


def _override_rate_limit_redis(app: FastAPI) -> None:
    async def _override_redis() -> AsyncGenerator[Redis, None]:
        client = Redis.from_url(_TEST_REDIS_URL, decode_responses=True)
        await client.flushdb()
        try:
            yield client
        finally:
            await client.aclose()

    app.dependency_overrides[get_redis] = _override_redis


# Fixed id for the overridden authenticated test user.
TEST_USER_ID: int = 7001

# Fixed id for a second, unrelated user (owns "foreign" saved addresses).
OTHER_USER_ID: int = 9999


def _build_app(db_session: AsyncSession) -> FastAPI:
    """Assemble a minimal app mounting cart + checkout + orders routers."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(cart_router, prefix=_API_V1_PREFIX)
    app.include_router(checkout_router, prefix=_API_V1_PREFIX)
    app.include_router(orders_router, prefix=_API_V1_PREFIX)

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    _override_rate_limit_redis(app)
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
        email="checkout-user@example.com",
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
    """Authenticated-path client with auth deps -> the test user."""
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
async def other_user(db_session: AsyncSession) -> AppUser:
    """Persist a second, unrelated user (owner of "foreign" addresses)."""
    user = AppUser(
        id=OTHER_USER_ID,
        email="other-user@example.com",
        password_hash="x",
        is_active=True,
        is_staff=False,
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest_asyncio.fixture
async def add_address(db_session: AsyncSession, test_user: AppUser):
    """Return a helper inserting an :class:`Address` for the courier path.

    Depends on ``test_user`` so the default owner exists; a foreign ``user_id``
    (e.g. :data:`OTHER_USER_ID`) requires the caller to also request the
    ``other_user`` fixture so its ``app_user`` row exists (FK).
    """

    async def _add(address_id: int, *, user_id: int = TEST_USER_ID) -> Address:
        address = Address(
            id=address_id,
            user_id=user_id,
            full_name="Test Buyer",
            phone="+3730",
            city="Chisinau",
            street="Str. 1",
        )
        db_session.add(address)
        await db_session.flush()
        return address

    return _add


@pytest_asyncio.fixture
async def add_product(db_session: AsyncSession):
    """Return a helper inserting an active product + ``ro`` translation.

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
