"""Celery tasks for the Telegram support module (privacy retention).

* ``support.purge_stale`` — scheduled sweep that deletes support conversations
  inactive past the retention window (``settings.support_retention_days``),
  their messages cascading. This is the storage-limitation control (LP195/2024
  Art.5): Telegram support data is not account-linked, so account erasure cannot
  reach it — retention expiry (here) and per-conversation admin delete are the
  two erasure paths.

Uses :func:`app.tasks.base.run_async_session` (a fresh NullPool engine on a
fresh event loop per call — fork-safe) and delegates to
:meth:`SupportService.purge_stale`. The service is built without a Redis client
(``redis=None``): a batch purge publishes no per-row live events.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.celery_app import celery_app
from app.core.config import settings
from app.services.support_service import SupportService
from app.tasks.base import run_async_session


async def _purge(session: AsyncSession) -> int:
    """Purge support conversations inactive past the retention window.

    Args:
        session: Task-scoped async session.

    Returns:
        int: The number of conversations deleted.
    """
    return await SupportService(session).purge_stale(settings.support_retention_days)


@celery_app.task(name="support.purge_stale")
def purge_stale_conversations() -> int:
    """Delete support conversations inactive past the retention window (LP195 Art.5).

    Returns:
        int: The number of conversations deleted.
    """
    return run_async_session(_purge)
