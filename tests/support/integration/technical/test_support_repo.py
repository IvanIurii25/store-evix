"""Direct repository tests for :class:`SupportRepository` (support module).

Exercises the query/write methods against the transactional ``db_session``
(no HTTP, no service layer): conversation create/lookup, message add + dedup
existence check, inbox listing (ordering + status filter + count), thread
paging (chronological + count), status set (present/absent), mark_read, and the
inbound/outbound ``bump_activity`` branches.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.support import SupportConversation
from app.repositories.support_repo import SupportRepository

pytestmark = pytest.mark.asyncio

# Telegram chat ids used across the repo tests (BigInteger-sized values are
# fine; small ints keep the assertions readable).
_CHAT_A: int = 1001
_CHAT_B: int = 1002
_CHAT_C: int = 1003
# A fixed base time; the inbox-ordering tests set explicit ``last_message_at``
# values because ``func.now()`` is transaction-constant and cannot distinguish
# rows written in the same test transaction.
_BASE_TIME: datetime = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


async def _new_conversation(
    repo: SupportRepository,
    *,
    tg_chat_id: int,
    name: str | None = "Ana",
    username: str | None = "ana",
    lang: str | None = "ro",
) -> SupportConversation:
    """Create a conversation through the repo (keeps arrange sections short)."""
    return await repo.create_conversation(
        tg_chat_id=tg_chat_id,
        customer_name=name,
        customer_username=username,
        lang=lang,
    )


class SupportRepositoryConversationTest:
    """``create_conversation`` / ``get_by_tg_chat_id`` / ``get_conversation``."""

    async def test_create_and_lookup_by_chat_id(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: a repo bound to the test session.
        repo = SupportRepository(db_session)

        # Act: create a conversation, then look it up by chat id.
        created = await _new_conversation(repo, tg_chat_id=_CHAT_A)
        found = await repo.get_by_tg_chat_id(_CHAT_A)

        # Assert: same row, with DB defaults applied (open, unread 0).
        assert created.id is not None, "new conversation must be flushed"
        assert found is not None, "conversation must be found by chat id"
        assert found.id == created.id, "lookup must return the created row"
        assert found.status == "open", "new conversation defaults to open"
        assert found.unread_count == 0, "new conversation starts unread=0"

    async def test_get_by_unknown_chat_id_returns_none(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: an empty repo.
        repo = SupportRepository(db_session)

        # Act + Assert: an unseen chat id has no conversation.
        assert await repo.get_by_tg_chat_id(999999) is None, "unknown chat → None"

    async def test_get_conversation_present_and_absent(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: one conversation.
        repo = SupportRepository(db_session)
        conv = await _new_conversation(repo, tg_chat_id=_CHAT_A)

        # Act + Assert: present id resolves, unknown id is None.
        assert (await repo.get_conversation(conv.id)).id == conv.id, "present → row"
        assert await repo.get_conversation(999999) is None, "absent → None"


class SupportRepositoryMessageTest:
    """``add_message`` + ``message_exists`` dedup check + thread reads."""

    async def test_add_message_and_exists(self, db_session: AsyncSession) -> None:
        # Arrange: a conversation to attach a message to.
        repo = SupportRepository(db_session)
        conv = await _new_conversation(repo, tg_chat_id=_CHAT_A)

        # Act: append an inbound message carrying a Telegram message id.
        msg = await repo.add_message(
            conversation_id=conv.id,
            direction="in",
            text="Salut",
            tg_message_id=500,
        )

        # Assert: the row persisted and the dedup check sees its tg id.
        assert msg.id is not None, "message must be flushed"
        assert await repo.message_exists(conv.id, 500) is True, "existing tg id seen"
        assert (
            await repo.message_exists(conv.id, 501) is False
        ), "unseen tg id must not match"

    async def test_get_thread_chronological_and_count(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: three messages inserted in order (created_at ascending).
        repo = SupportRepository(db_session)
        conv = await _new_conversation(repo, tg_chat_id=_CHAT_A)
        for idx, body in enumerate(("first", "second", "third")):
            await repo.add_message(
                conversation_id=conv.id,
                direction="in",
                text=body,
                tg_message_id=600 + idx,
            )

        # Act: read the whole thread + its count.
        thread = await repo.get_thread(conv.id, offset=0, limit=50)
        total = await repo.count_thread(conv.id)

        # Assert: chronological order preserved and total is accurate.
        assert [m.text for m in thread] == [
            "first",
            "second",
            "third",
        ], "thread must be chronological (created_at asc)"
        assert total == 3, "count_thread must count every message"


class SupportRepositoryInboxTest:
    """Inbox listing: ordering, status filter, count."""

    async def test_list_orders_by_last_message_desc(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: two conversations with explicit, distinct last_message_at.
        # ``func.now()`` is transaction-constant, so ordering is proven by
        # setting concrete timestamps rather than relying on bump timing.
        repo = SupportRepository(db_session)
        older = await _new_conversation(repo, tg_chat_id=_CHAT_A)
        newer = await _new_conversation(repo, tg_chat_id=_CHAT_B)
        older.last_message_at = _BASE_TIME
        newer.last_message_at = _BASE_TIME + timedelta(minutes=5)
        await db_session.flush()

        # Act: list the inbox (no filter).
        rows = await repo.list_conversations(status=None, offset=0, limit=20)

        # Assert: newest activity first.
        assert rows[0].id == newer.id, "most-recent activity must sort first"
        assert {r.id for r in rows} == {older.id, newer.id}, "both must appear"

    async def test_status_filter_and_count(self, db_session: AsyncSession) -> None:
        # Arrange: one open + one closed conversation.
        repo = SupportRepository(db_session)
        open_conv = await _new_conversation(repo, tg_chat_id=_CHAT_A)
        closed_conv = await _new_conversation(repo, tg_chat_id=_CHAT_B)
        await repo.set_status(closed_conv.id, "closed")

        # Act: list + count filtered to "open".
        rows = await repo.list_conversations(status="open", offset=0, limit=20)
        open_count = await repo.count_conversations(status="open")
        all_count = await repo.count_conversations(status=None)

        # Assert: only the open row appears; counts respect the filter.
        assert [r.id for r in rows] == [open_conv.id], "only open must be listed"
        assert open_count == 1, "one open conversation"
        assert all_count == 2, "two conversations in total"

    async def test_list_pagination_offset_limit(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: three conversations with explicit ascending last_message_at.
        repo = SupportRepository(db_session)
        ids: list[int] = []
        for offset_min, chat in enumerate((_CHAT_A, _CHAT_B, _CHAT_C)):
            conv = await _new_conversation(repo, tg_chat_id=chat)
            conv.last_message_at = _BASE_TIME + timedelta(minutes=offset_min)
            ids.append(conv.id)
        await db_session.flush()

        # Act: take the second "page" of size 1 (offset 1).
        page = await repo.list_conversations(status=None, offset=1, limit=1)

        # Assert: exactly one row, and it is the middle one by recency.
        assert len(page) == 1, "limit=1 returns one row"
        # ids[2] is newest → offset 1 skips it and returns ids[1].
        assert page[0].id == ids[1], "offset must skip the newest row"


class SupportRepositoryMutationTest:
    """``set_status`` / ``mark_read`` / ``bump_activity`` branches."""

    async def test_set_status_present_and_absent(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: one conversation to transition.
        repo = SupportRepository(db_session)
        conv = await _new_conversation(repo, tg_chat_id=_CHAT_A)

        # Act: set a real one, and attempt an unknown one.
        updated = await repo.set_status(conv.id, "closed")
        missing = await repo.set_status(999999, "closed")

        # Assert: present flips status; absent returns None.
        assert updated is not None and updated.status == "closed", "status set"
        assert missing is None, "unknown conversation → None"

    async def test_mark_read_zeroes_unread(self, db_session: AsyncSession) -> None:
        # Arrange: a conversation with a non-zero unread counter.
        repo = SupportRepository(db_session)
        conv = await _new_conversation(repo, tg_chat_id=_CHAT_A)
        await repo.bump_activity(conv, inbound=True)
        await db_session.flush()
        await db_session.refresh(conv)
        assert conv.unread_count == 1, "arrange: unread must be 1 before mark_read"

        # Act: operator opens the conversation.
        await repo.mark_read(conv.id)

        # Assert: unread reset to zero (re-read from the DB).
        refreshed = (
            await db_session.execute(
                select(SupportConversation).where(SupportConversation.id == conv.id)
            )
        ).scalar_one()
        assert refreshed.unread_count == 0, "mark_read must zero unread_count"

    async def test_bump_inbound_increments_unread(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: a fresh conversation (unread 0).
        repo = SupportRepository(db_session)
        conv = await _new_conversation(repo, tg_chat_id=_CHAT_A)

        # Act: bump twice on inbound messages.
        await repo.bump_activity(conv, inbound=True)
        await repo.bump_activity(conv, inbound=True)
        await db_session.flush()
        await db_session.refresh(conv)

        # Assert: unread grew by two and last_message_at is (re)stamped.
        # ``func.now()`` is transaction-constant, so the value cannot advance
        # within one transaction; assert it is populated, not that it moved.
        assert conv.unread_count == 2, "each inbound bump increments unread"
        assert conv.last_message_at is not None, "inbound bump stamps last_message_at"

    async def test_bump_outbound_only_bumps_activity(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: a conversation with one unread inbound already recorded.
        repo = SupportRepository(db_session)
        conv = await _new_conversation(repo, tg_chat_id=_CHAT_A)
        await repo.bump_activity(conv, inbound=True)
        await db_session.flush()
        await db_session.refresh(conv)
        unread_before = conv.unread_count

        # Act: an outbound (operator) bump.
        await repo.bump_activity(conv, inbound=False)
        await db_session.flush()
        await db_session.refresh(conv)

        # Assert: unread unchanged (operator reply is not an unread inbound).
        assert conv.unread_count == unread_before, "outbound must not touch unread"
