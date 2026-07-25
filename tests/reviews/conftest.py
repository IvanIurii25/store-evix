"""Reviews & Ratings test fixtures (Phase 1, §7).

Wires a *local* FastAPI app mounting the customer reviews router, the admin
reviews router and the catalog router (for the ``ProductOut.rating_*`` assertions)
under ``/api/v1``, with:

* ``get_session`` overridden to the shared commit-safe ``db_session`` (write
  paths commit; ``db_session`` uses a restarting SAVEPOINT so the outer
  transaction is still rolled back on teardown → full isolation);
* the unified error handlers registered;
* ``current_user`` overridden to a fixed persisted customer (``client``) and
  ``current_staff`` to a staff user (``staff_client``), plus a no-override
  ``guest_client`` to exercise the 401 guard.

Seeding helpers persist a category + translated product, users, and a
non-cancelled order line (for the verified-purchase snapshot).

Run with ``EVIX_TEST_DB=evix_test_reviews``.
"""

from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff, current_user
from app.api.routers.admin_reviews import router as admin_reviews_router
from app.api.routers.catalog import router as catalog_router
from app.api.routers.reviews import router as reviews_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.models.catalog import Category, Product, ProductTranslation
from app.models.order import Order, OrderItem
from app.models.user import AppUser

_API_V1_PREFIX = "/api/v1"

# Fixed ids for the overridden authenticated users.
TEST_USER_ID: int = 7001
TEST_OTHER_USER_ID: int = 7003
TEST_STAFF_ID: int = 7002


def _build_app(db_session: AsyncSession) -> FastAPI:
    """Assemble a minimal app mounting the reviews + admin + catalog routers."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(reviews_router, prefix=_API_V1_PREFIX)
    app.include_router(admin_reviews_router, prefix=_API_V1_PREFIX)
    app.include_router(catalog_router, prefix=_API_V1_PREFIX)

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    return app


@pytest_asyncio.fixture
async def customer(db_session: AsyncSession) -> AppUser:
    """Persist and return a fixed authenticated customer."""
    user = AppUser(
        id=TEST_USER_ID,
        email="reviewer@example.com",
        password_hash="x",
        is_active=True,
        is_staff=False,
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest_asyncio.fixture
async def other_user(db_session: AsyncSession) -> AppUser:
    """Persist and return a second customer (for the author-only delete guard)."""
    user = AppUser(
        id=TEST_OTHER_USER_ID,
        email="other@example.com",
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
        email="review-staff@example.com",
        password_hash="x",
        is_active=True,
        is_staff=True,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def make_user(db_session: AsyncSession, user_id: int) -> AppUser:
    """Persist and return a minimal user with a specific id (for FK targets).

    Args:
        db_session: The test session.
        user_id: The primary key to assign (tests use distinct ids per review so
            the ``UNIQUE (product_id, user_id)`` constraint is not tripped).

    Returns:
        AppUser: The persisted user.
    """
    user = AppUser(
        id=user_id,
        email=f"seed-{user_id}@example.com",
        password_hash="x",
        is_active=True,
        is_staff=False,
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
    slug: str = "phone",
) -> Product:
    """Persist an active product (+ ru/ro translations).

    Args:
        db_session: The test session.
        code: Unique product article code.
        slug: Base slug; the language is suffixed per translation.

    Returns:
        Product: The persisted product.
    """
    category = await _make_category(db_session)
    product = Product(
        category_id=category.id,
        code=code,
        price=Decimal("100"),
        qty=5,
        is_active=True,
    )
    db_session.add(product)
    await db_session.flush()
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


async def make_order_for(
    db_session: AsyncSession,
    *,
    user_id: int,
    product_id: int,
    number: str,
    status: str = "confirmed",
) -> Order:
    """Persist an order (+ one line for ``product_id``) owned by ``user_id``.

    Used to exercise the ``is_verified`` snapshot: a non-cancelled order line
    marks the reviewer as a verified purchaser (§2).

    Args:
        db_session: The test session.
        user_id: The purchasing user.
        product_id: The product bought (order-line ``product_id``).
        number: Unique order number.
        status: Fulfilment status (``canceled`` = not a purchase).

    Returns:
        Order: The persisted order.
    """
    order = Order(
        number=number,
        user_id=user_id,
        email="reviewer@example.com",
        phone="+37360000000",
        status=status,
        payment_status="pending",
        subtotal=Decimal("100"),
        discount_total=Decimal("0"),
        delivery_cost=Decimal("0"),
        total=Decimal("100"),
        delivery_type="pickup",
        payment_method="cod",
    )
    db_session.add(order)
    await db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.id,
            product_id=product_id,
            name_snapshot="Phone",
            price_snapshot=Decimal("100"),
            qty=1,
        )
    )
    await db_session.flush()
    return order


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
async def other_client(
    db_session: AsyncSession,
    other_user: AppUser,
) -> AsyncGenerator[AsyncClient, None]:
    """Second customer client (``current_user`` = the other user)."""
    app = _build_app(db_session)

    async def _override_user() -> AppUser:
        return other_user

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
