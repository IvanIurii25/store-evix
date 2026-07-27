"""Order-domain test fixtures (B6, adversarial HTTP tests).

These fixtures wire a *local* FastAPI app that mounts only the checkout, orders
and cart routers under ``/api/v1`` — the cart router is included purely to seed a
cart over HTTP the same way the storefront does (guest ``session_token`` cookie),
so checkout is always exercised against a cart built through the public contract,
never a hand-forged one.

Two independent client flavours are provided:

* ``client`` — guest path, bound to the shared commit-safe ``db_session`` (the
  checkout service commits; the session uses a restarting SAVEPOINT so the outer
  transaction is still rolled back on teardown → full isolation).
* ``user_client`` — the same app but with ``guest_or_user`` / ``current_user``
  overridden to a fixed persisted test user, exercising the authenticated path.

The router imports are **deferred** into ``_build_app`` so that
``pytest --collect-only`` still succeeds while the integrator's
``app.api.routers.checkout`` / ``.orders`` modules do not yet exist; a missing
module raises a clear ``pytest.skip`` at fixture setup rather than a collection
error. Once the integrator lands the routers, the fixtures resolve them for real.

For the last-unit race case a separate, non-rollback fixture (``real_engine``)
hands out real committing sessions on independent engines and truncates the
order tables afterwards — a SAVEPOINT-rolled-back session cannot exhibit a real
concurrent commit race, so that one test opts out of the shared isolation.
"""

from collections.abc import AsyncGenerator, Callable
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.deps import current_user, guest_or_user
from app.api.routers.cart import router as cart_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.core.redis import get_redis
from app.models.catalog import Category, Product, ProductTranslation
from app.models.user import AppUser
from tests.conftest import TEST_URL

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
TEST_USER_ID: int = 8001


def _import_order_routers() -> tuple[object, object]:
    """Import the integrator's checkout + orders routers, or skip cleanly.

    The routers are written in parallel by the implementation agent. If they are
    not present yet, skip the dependent test with an explicit message instead of
    failing collection.

    Returns:
        tuple: ``(checkout_router, orders_router)``.
    """
    try:
        from app.api.routers.checkout import router as checkout_router
        from app.api.routers.orders import router as orders_router
    except ModuleNotFoundError as exc:  # pragma: no cover - integrator dependency
        pytest.skip(f"checkout/orders routers not available yet: {exc}")
    return checkout_router, orders_router


def _build_app(db_session: AsyncSession) -> FastAPI:
    """Assemble a minimal app mounting cart + checkout + orders routers.

    Args:
        db_session: The commit-safe session to bind ``get_session`` to.

    Returns:
        FastAPI: The configured application.
    """
    checkout_router, orders_router = _import_order_routers()
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
        email="order-user@example.com",
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
    """Authenticated-path client with auth deps -> the fixed test user."""
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
async def add_product(db_session: AsyncSession) -> Callable:
    """Return a helper inserting an active product + ``ro`` translation.

    Ensures a parent category (id=1) exists first (``product.category_id`` FK).

    Returns:
        Callable: ``_add(product_id, *, price, code, name=..., qty=..., ...)``.
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


# --------------------------------------------------------------------------- #
# Real-commit harness for the last-unit race (opts out of SAVEPOINT isolation)
# --------------------------------------------------------------------------- #
_RACE_TABLES: tuple[str, ...] = (
    "payment",
    '"order_status_history"',
    '"order_item"',
    '"order"',
    "cart_item",
    "cart",
    "product_attribute",
    "media",
    "product_translation",
    "product_card",
    "product",
    "category_translation",
    "category",
    "promo_code",
    "app_user",
)


@pytest_asyncio.fixture
async def real_engine() -> AsyncGenerator[Callable, None]:
    """Provide a factory of *real committing* sessions on independent engines.

    Needed for the concurrent last-unit race: the shared ``db_session`` rolls
    everything back in a SAVEPOINT, which cannot reproduce two competing real
    commits. Each call returns a fresh engine + sessionmaker bound to the same
    physical test database (``TEST_URL``); teardown truncates the touched tables
    so the case leaves no residue for the rest of the suite.

    Yields:
        Callable: ``make() -> (engine, async_sessionmaker)``.
    """
    engines = []

    def _make():
        engine = create_async_engine(TEST_URL, poolclass=None)
        engines.append(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        return engine, factory

    try:
        yield _make
    finally:
        cleanup = create_async_engine(TEST_URL, isolation_level="AUTOCOMMIT")
        async with cleanup.connect() as conn:
            for table in _RACE_TABLES:
                await conn.execute(text(f"TRUNCATE {table} RESTART IDENTITY CASCADE"))
        await cleanup.dispose()
        for engine in engines:
            await engine.dispose()
