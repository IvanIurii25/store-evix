"""Trivial health tasks proving the Celery wiring.

* ``health.ping`` — pure sync, no DB. Proves broker → worker → result-backend.
* ``health.db_check`` — proves the async-DB-in-worker pattern end to end.

``db_check`` is split into a thin task wrapper and an async impl (``_db_check``)
so the impl can be unit-tested directly with a test session, without going
through :func:`app.tasks.base.run_async_session` (which calls ``asyncio.run`` and
cannot run inside an already-running event loop).
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.celery_app import celery_app
from app.tasks.base import run_async_session


@celery_app.task(name="health.ping")
def ping() -> str:
    """Return ``"pong"`` — a DB-free liveness probe for the broker/worker path.

    Returns:
        The literal string ``"pong"``.
    """
    return "pong"


async def _db_check(session: AsyncSession) -> int:
    """Execute ``SELECT 1`` and return the scalar result.

    Args:
        session: An async session to run the probe query on.

    Returns:
        The integer ``1`` if the database round-trip succeeds.
    """
    return int((await session.execute(text("SELECT 1"))).scalar_one())


@celery_app.task(name="health.db_check")
def db_check() -> int:
    """Prove the async-DB-in-worker pattern by running ``SELECT 1``.

    Returns:
        The integer ``1`` on a successful database round-trip.
    """
    return run_async_session(_db_check)
