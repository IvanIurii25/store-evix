"""Privacy-retention tests for the support module (service + task layers).

Covers the two erasure paths introduced for LP195/2024 (Art.5 storage
limitation, Art.17 on-request erasure):

* :meth:`SupportService.purge_stale` — the batch retention sweep, exercised on
  the Optional-redis path (``SupportService(session)`` with no Redis client, as
  the Celery task builds it) to prove a bulk purge never needs the pub/sub
  client. Asserts it deletes only conversations past the window, returns the
  purged count and commits (the fresh row survives).
* :meth:`SupportService.delete_conversation` — the on-request single erasure,
  driven with an isolated Redis client so the best-effort ``status`` publish
  runs; an unknown id raises :class:`ConversationNotFoundError`.
* :func:`app.tasks.support._purge` — the async helper the Celery task wraps,
  exercised directly against a session (no worker) with a monkeypatched
  low retention window.
"""

from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

import app.tasks.support as support_task
from app.repositories.support_repo import SupportRepository
from app.services.support_service import (
    ConversationNotFoundError,
    SupportService,
)

pytestmark = pytest.mark.asyncio

# Telegram chat ids for seeded conversations (kept small for readable asserts).
_CHAT_STALE: int = 3001
_CHAT_FRESH: int = 3002
_CHAT_ERASE: int = 3003
# Retention window used by the service-level purge tests.
_RETENTION_DAYS: int = 30
# One conversation is inactive well past the window, the other well within it.
_OLD_AGE_DAYS: int = 40
_RECENT_AGE_DAYS: int = 10


async def _seed_conversation(
    session: AsyncSession,
    *,
    tg_chat_id: int,
    age_days: int,
) -> int:
    """Persist a conversation with ``last_message_at`` set ``age_days`` ago.

    ``func.now()`` is transaction-constant, so retention tests set an explicit
    wall-clock ``last_message_at`` (the service's cutoff is derived from
    ``datetime.now(UTC)``, not the transaction clock).

    Args:
        session: The active test session.
        tg_chat_id: The Telegram chat id for the conversation.
        age_days: How many days ago the conversation was last active.

    Returns:
        int: The persisted conversation's primary key.
    """
    repo = SupportRepository(session)
    conv = await repo.create_conversation(
        tg_chat_id=tg_chat_id,
        customer_name="Ana",
        customer_username="ana",
        lang="ro",
    )
    conv.last_message_at = datetime.now(UTC) - timedelta(days=age_days)
    await session.flush()
    return conv.id


class SupportServicePurgeStaleTest:
    """``SupportService.purge_stale`` — batch retention sweep (no Redis)."""

    async def test_purge_removes_stale_keeps_fresh_and_returns_count(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: one conversation inactive past the window, one within it.
        # The service is built WITHOUT redis (the batch path is Optional-redis).
        stale_id = await _seed_conversation(
            db_session, tg_chat_id=_CHAT_STALE, age_days=_OLD_AGE_DAYS
        )
        fresh_id = await _seed_conversation(
            db_session, tg_chat_id=_CHAT_FRESH, age_days=_RECENT_AGE_DAYS
        )
        service = SupportService(db_session)

        # Act: purge conversations inactive longer than the retention window.
        deleted = await service.purge_stale(_RETENTION_DAYS)

        # Assert: only the stale row is purged; the count reflects that.
        assert deleted == 1, "exactly one conversation is past the retention window"
        repo = SupportRepository(db_session)
        assert await repo.get_conversation(stale_id) is None, "stale row purged"
        assert (
            await repo.get_conversation(fresh_id) is not None
        ), "the recently-active conversation must survive"

    async def test_purge_commits_the_deletion(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: a single stale conversation.
        stale_id = await _seed_conversation(
            db_session, tg_chat_id=_CHAT_STALE, age_days=_OLD_AGE_DAYS
        )
        service = SupportService(db_session)

        # Act: purge, then expire the identity map to force a fresh DB read.
        await service.purge_stale(_RETENTION_DAYS)
        db_session.expire_all()

        # Assert: the row is absent on re-read — the purge committed, it was not
        # left pending in the unit of work.
        repo = SupportRepository(db_session)
        assert (
            await repo.get_conversation(stale_id) is None
        ), "purge_stale must commit the deletion"


class SupportServiceDeleteConversationTest:
    """``SupportService.delete_conversation`` — on-request erasure (Redis)."""

    async def test_delete_removes_conversation_and_commits(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a conversation to erase; the service has a live Redis client
        # so the best-effort "status" publish path is exercised too.
        conv_id = await _seed_conversation(
            db_session, tg_chat_id=_CHAT_ERASE, age_days=_RECENT_AGE_DAYS
        )
        service = SupportService(db_session, redis_client)

        # Act: erase the conversation on request.
        await service.delete_conversation(conv_id)
        db_session.expire_all()

        # Assert: the row is gone on a fresh read (delete committed).
        repo = SupportRepository(db_session)
        assert (
            await repo.get_conversation(conv_id) is None
        ), "delete_conversation must remove and commit the row"

    async def test_delete_unknown_id_raises_not_found(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a service over an empty inbox.
        service = SupportService(db_session, redis_client)

        # Act + Assert: erasing a missing conversation is a domain 404.
        with pytest.raises(ConversationNotFoundError):
            await service.delete_conversation(999999)


class SupportPurgeTaskTest:
    """``app.tasks.support._purge`` — the async helper the Celery task wraps."""

    async def test_purge_helper_returns_int_count(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: a tiny retention window (1 day) and a conversation older than
        # it, so the helper has something to purge. ``_purge`` reads the window
        # off ``settings.support_retention_days``.
        monkeypatch.setattr(support_task.settings, "support_retention_days", 1)
        stale_id = await _seed_conversation(
            db_session, tg_chat_id=_CHAT_STALE, age_days=_OLD_AGE_DAYS
        )

        # Act: run the async helper directly against the test session (no worker,
        # no fresh engine — we exercise the wrapped coroutine, not run_async_session).
        deleted = await support_task._purge(db_session)

        # Assert: an integer purge count, and the stale row is actually gone.
        assert isinstance(deleted, int), "_purge must return an int count"
        assert deleted == 1, "the single over-retention conversation is purged"
        repo = SupportRepository(db_session)
        assert (
            await repo.get_conversation(stale_id) is None
        ), "_purge must delete the stale conversation"
