"""Admin-orders test fixtures (B7, §10).

Wires a *local* FastAPI app that mounts only the admin-orders router under
``/api/v1``, with:

* ``get_session`` overridden to the shared commit-safe ``db_session`` (the
  transition path commits; ``db_session`` uses a restarting SAVEPOINT so the
  outer transaction is still rolled back on teardown → full isolation);
* the unified error handlers registered (so domain errors render the shared
  ``{error:{code,message,details?}}`` envelope);
* ``current_staff`` overridden to a fixed persisted staff user.

Two client flavours are provided: ``client`` (staff, the normal admin path) and
``guest_client`` (no staff override → the real ``current_staff`` dependency runs
and the guard is exercised — 401 without a token).

Orders are seeded directly through the ``Order`` / ``OrderItem`` models (the
``make_order`` factory) — the admin API is read/transition only, so building
fixtures via the checkout flow would be needless coupling.

Run with ``EVIX_TEST_DB=evix_test_admin_orders``.
"""

from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.api.routers.admin_orders import router as admin_orders_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.core.redis import get_redis
from app.models.order import Order, OrderItem
from app.models.user import AppUser

_API_V1_PREFIX = "/api/v1"

# Fixed id for the overridden authenticated staff user.
TEST_STAFF_ID: int = 9001


def _build_app(db_session: AsyncSession) -> FastAPI:
    """Assemble a minimal app mounting the admin-orders router.

    Args:
        db_session: The commit-safe session to bind ``get_session`` to.

    Returns:
        FastAPI: The configured application.
    """
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(admin_orders_router, prefix=_API_V1_PREFIX)

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _override_redis():
        # The waybill endpoints need a Redis; an isolated db keeps carrier token
        # keys away from the rate-limit and cache tests.
        client = Redis.from_url("redis://localhost:56379/12", decode_responses=True)
        try:
            yield client
        finally:
            await client.aclose()

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_redis] = _override_redis
    return app


@pytest_asyncio.fixture
async def staff_user(db_session: AsyncSession) -> AppUser:
    """Persist and return a fixed authenticated staff user."""
    user = AppUser(
        id=TEST_STAFF_ID,
        email="admin-orders@example.com",
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
async def guest_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """No-auth client: the real ``current_staff`` dependency runs (guard test)."""
    app = _build_app(db_session)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def make_order(db_session: AsyncSession) -> Callable:
    """Return a helper inserting an ``Order`` (+ one line) directly via models.

    Returns:
        Callable: ``_make(number, *, email=..., phone=..., status=..., ...)``
            → the persisted :class:`Order`.
    """

    async def _make(
        number: str,
        *,
        email: str = "buyer@example.com",
        phone: str = "+37360000000",
        status: str = "new",
        payment_status: str = "pending",
        total: Decimal = Decimal("100.00"),
    ) -> Order:
        order = Order(
            number=number,
            user_id=None,
            email=email,
            phone=phone,
            status=status,
            payment_status=payment_status,
            subtotal=total,
            discount_total=Decimal("0"),
            delivery_cost=Decimal("0"),
            total=total,
            delivery_type="pickup",
            delivery_address_id=None,
            payment_method="cod",
        )
        db_session.add(order)
        await db_session.flush()
        db_session.add(
            OrderItem(
                order_id=order.id,
                product_id=None,
                name_snapshot="Sample product",
                price_snapshot=total,
                qty=1,
            )
        )
        await db_session.flush()
        return order

    return _make


@pytest_asyncio.fixture
def _utc_now() -> datetime:
    """Convenience timestamp (kept for future history assertions)."""
    return datetime.now(UTC)
