"""Shared test harness.

Provides a migrated, isolated PostgreSQL test database and fixtures:

* ``db_session`` — an :class:`AsyncSession` wrapped in a transaction that is
  rolled back after each test (full isolation, no cross-test pollution).
* ``async_client`` — an httpx ``AsyncClient`` bound to the app with
  ``get_session`` overridden to the test session.

The test DB name comes from ``EVIX_TEST_DB`` (default ``evix_test``) so parallel
agents/domains can each use a dedicated DB on the same server without clashing.
The schema is created by running the real Alembic migration once per session.

Each async fixture creates its own engine on the running test's event loop
(pytest-asyncio uses a per-test loop), which avoids "event loop is closed"
errors from sharing a session-scoped async resource across loops.
"""

import asyncio
import os
import subprocess

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DB = os.environ.get("EVIX_TEST_DB", "evix_test")
_HOST = "postgresql+asyncpg://evix:evix@localhost:55432"
ADMIN_URL = f"{_HOST}/postgres"
TEST_URL = f"{_HOST}/{TEST_DB}"


async def _create_db_if_absent() -> None:
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TEST_DB},
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    finally:
        await admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def _prepare_database():
    """Create the dedicated test DB (if absent) and migrate it once per session.

    Sync fixture: the async DB-creation runs in its own short-lived loop that is
    fully closed before any test loop starts.
    """
    asyncio.run(_create_db_if_absent())
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        check=True,
        env={**os.environ, "DATABASE_URL": TEST_URL},
        cwd=os.path.dirname(os.path.dirname(__file__)),
    )
    yield


@pytest.fixture(autouse=True)
def _reset_redis_singleton():
    """Drop the process-wide Redis client reference after every test.

    ``app.core.redis`` caches a module-level client bound to the loop it was
    first used on. pytest-asyncio uses a per-test loop, so a client created in
    one test would blow up (``Event loop is closed``) when a later test's app
    shutdown calls ``close_redis()``. Resetting the reference keeps each test's
    Redis usage loop-local.
    """
    yield
    import app.core.redis as _redis_module

    _redis_module._redis_client = None


@pytest_asyncio.fixture
async def db_session():
    """Yield a session inside a transaction rolled back after the test.

    Uses ``join_transaction_mode="create_savepoint"`` so a service that calls
    ``session.commit()`` only releases a SAVEPOINT — the outer transaction still
    owns everything and is rolled back on teardown, keeping full test isolation
    even for write/commit code paths (cart, checkout, admin).
    """
    engine = create_async_engine(TEST_URL)
    conn = await engine.connect()
    trans = await conn.begin()
    factory = async_sessionmaker(
        bind=conn,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    session = factory()
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def async_client(db_session):
    """httpx client bound to the app, with ``get_session`` -> the test session.

    Also overrides ``get_redis`` to an isolated db (14, flushed per request) so
    rate-limited routes (checkout/login) never trip a limit and never touch the
    process-wide prod Redis client across event loops.
    """
    from redis.asyncio import Redis

    from app.core.db import get_session
    from app.core.redis import get_redis
    from app.main import app

    async def _override_session():
        yield db_session

    async def _override_redis():
        client = Redis.from_url("redis://localhost:56379/14", decode_responses=True)
        await client.flushdb()
        try:
            yield client
        finally:
            await client.aclose()

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_redis] = _override_redis
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
