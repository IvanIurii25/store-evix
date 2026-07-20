"""Async database engine, session factory and FastAPI session dependency."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

engine: AsyncEngine = create_async_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
)

session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async session for the duration of a request.

    Yields:
        AsyncSession: A session bound to the shared engine pool. The session is
            closed automatically when the request ends.
    """
    async with session_factory() as session:
        yield session


async def dispose_engine() -> None:
    """Dispose the engine and close all pooled connections (called on shutdown)."""
    await engine.dispose()
