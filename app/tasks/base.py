"""Bridge for running async DB work from sync Celery tasks.

A Celery prefork worker runs tasks synchronously in forked child processes. The
app's shared engine (``app.core.db.engine``) is created at import time on the
parent process, so its asyncpg connection pool is inherited across ``fork`` and
is NOT safe to reuse in a child. Reusing it corrupts connection state.

:func:`run_async_session` sidesteps this entirely: each call builds a throwaway
async engine on a fresh event loop, runs the operation, and disposes the engine.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import settings

T = TypeVar("T")


def run_async_session(op: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run an async DB operation from a sync Celery task, fully isolated.

    Builds a FRESH async engine (``NullPool`` — connections are opened and
    closed per use, so nothing is pooled and nothing can leak across tasks or
    survive a ``fork``) and a session on a fresh event loop via
    :func:`asyncio.run`, runs ``op``, and disposes the engine in a ``finally``.

    This deliberately avoids ``app.core.db.engine`` (fork-unsafe pool in a
    prefork worker) and never shares an event loop between task invocations.

    Args:
        op: An async callable taking an :class:`AsyncSession` and returning a
            value. It owns its own commit/rollback semantics.

    Returns:
        The value returned by ``op``.
    """

    async def _runner() -> T:
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            maker = async_sessionmaker(
                engine, expire_on_commit=False, autoflush=False
            )
            async with maker() as session:
                return await op(session)
        finally:
            await engine.dispose()

    return asyncio.run(_runner())
