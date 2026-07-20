"""Local test harness for the auth domain (B4).

Builds a minimal FastAPI app wiring only the auth + users routers. Because the
auth service **commits** (unlike the read-only catalog service), the test session
must survive commits without leaking across tests. We therefore run each test
inside an outer transaction on a dedicated connection and use the SQLAlchemy
"restart SAVEPOINT on commit" pattern: the service's ``commit()`` releases a
nested savepoint while the outer transaction is rolled back at teardown, keeping
full per-test isolation.

Redis is overridden to an isolated db (15), flushed before each test.
"""

from collections.abc import AsyncGenerator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import get_auth_service
from app.api.routers.auth import router as auth_router
from app.api.routers.users import router as users_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.core.redis import get_redis
from app.services.auth_service import AuthService
from tests.conftest import TEST_URL

_TEST_REDIS_URL = "redis://localhost:56379/15"
_API_V1_PREFIX = "/api/v1"


@pytest_asyncio.fixture
async def tx_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a session that isolates commits via a restarting SAVEPOINT.

    The outer transaction is rolled back after the test, so committed work never
    persists between tests.
    """
    engine = create_async_engine(TEST_URL)
    conn = await engine.connect()
    outer = await conn.begin()
    factory = async_sessionmaker(bind=conn, expire_on_commit=False, autoflush=False)
    session = factory()
    await session.begin_nested()

    sync_session = session.sync_session

    @event.listens_for(sync_session, "after_transaction_end")
    def _restart_savepoint(sess, transaction):  # noqa: ANN001, ANN202
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    try:
        yield session
    finally:
        event.remove(sync_session, "after_transaction_end", _restart_savepoint)
        await session.close()
        if outer.is_active:
            await outer.rollback()
        await conn.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def redis_client() -> AsyncGenerator[Redis, None]:
    """Yield an isolated Redis client (db 15), flushed before each test."""
    client = Redis.from_url(_TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def db_session(tx_session: AsyncSession) -> AsyncSession:
    """Alias so tests reading rows use the same committing session."""
    return tx_session


@pytest_asyncio.fixture
async def client(
    tx_session: AsyncSession,
    redis_client: Redis,
) -> AsyncGenerator[AsyncClient, None]:
    """httpx client bound to a minimal app with session + redis overridden."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(auth_router, prefix=_API_V1_PREFIX)
    app.include_router(users_router, prefix=_API_V1_PREFIX)

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield tx_session

    async def _override_redis() -> AsyncGenerator[Redis, None]:
        yield redis_client

    def _override_service() -> AuthService:
        return AuthService(session=tx_session, redis=redis_client)

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_redis] = _override_redis
    app.dependency_overrides[get_auth_service] = _override_service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.clear()
