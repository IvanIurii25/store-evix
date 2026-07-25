"""Integration tests for ``SupportService.handle_inbound`` return value.

The webhook debounces staff pings to one per unread burst: ``handle_inbound``
returns the ``conversation_id`` only when a message *starts* a fresh unread burst
(a brand-new conversation, or an existing one whose ``unread_count`` was 0), and
``None`` otherwise (a follow-up while already unread, or a de-duplicated
re-delivery). These tests drive the service with :class:`InboundMessage` objects
directly against the real session + isolated Redis.
"""

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.telegram import InboundMessage
from app.repositories.support_repo import SupportRepository
from app.services.support_service import SupportService

pytestmark = pytest.mark.asyncio

# Telegram chat ids for the scenarios (kept small for readable asserts).
_CHAT_NEW: int = 4101
_CHAT_READ: int = 4102
_CHAT_UNREAD: int = 4103
_CHAT_DUP: int = 4104
# Telegram message ids used across the inbound builders.
_MSG_FIRST: int = 10
_MSG_SECOND: int = 11


def _inbound(*, chat_id: int, message_id: int) -> InboundMessage:
    """Return a parsed private text inbound for the given chat + message id."""
    return InboundMessage(
        chat_id=chat_id,
        message_id=message_id,
        text="Salut",
        customer_name="Ana",
        customer_username="ana",
        lang="ro",
    )


async def _seed_conversation(
    session: AsyncSession,
    *,
    tg_chat_id: int,
    unread_count: int,
) -> int:
    """Persist a conversation with an explicit ``unread_count`` and return its id."""
    repo = SupportRepository(session)
    conv = await repo.create_conversation(
        tg_chat_id=tg_chat_id,
        customer_name="Ana",
        customer_username="ana",
        lang="ro",
    )
    conv.unread_count = unread_count
    await session.flush()
    return conv.id


class SupportServiceHandleInboundBurstTest:
    """``handle_inbound`` fresh-burst debounce: returns id vs ``None``."""

    async def test_new_conversation_returns_new_id(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: an unknown chat — no conversation exists yet.
        service = SupportService(db_session, redis_client)

        # Act: ingest the first-ever inbound for this chat.
        result = await service.handle_inbound(
            _inbound(chat_id=_CHAT_NEW, message_id=_MSG_FIRST)
        )

        # Assert: a brand-new conversation is a fresh burst → its id is returned.
        conv = await SupportRepository(db_session).get_by_tg_chat_id(_CHAT_NEW)
        assert result == conv.id, "a new conversation must return its id (fresh burst)"

    async def test_existing_read_conversation_returns_id(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: an existing conversation the operator has fully read (unread=0).
        conv_id = await _seed_conversation(
            db_session, tg_chat_id=_CHAT_READ, unread_count=0
        )
        service = SupportService(db_session, redis_client)

        # Act: a NEW inbound message arrives on the read conversation.
        result = await service.handle_inbound(
            _inbound(chat_id=_CHAT_READ, message_id=_MSG_FIRST)
        )

        # Assert: unread was 0 → this starts a fresh burst → the id is returned.
        assert result == conv_id, "a previously-read conversation restarts the burst"

    async def test_existing_unread_conversation_returns_none(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: an existing conversation already carrying unread messages.
        await _seed_conversation(db_session, tg_chat_id=_CHAT_UNREAD, unread_count=1)
        service = SupportService(db_session, redis_client)

        # Act: a follow-up inbound arrives while still unread.
        result = await service.handle_inbound(
            _inbound(chat_id=_CHAT_UNREAD, message_id=_MSG_FIRST)
        )

        # Assert: already unread → no re-ping (debounced) → None.
        assert result is None, "a follow-up while already unread must not re-ping"

    async def test_redelivered_update_returns_none(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a first inbound establishes the conversation + stored message.
        service = SupportService(db_session, redis_client)
        await service.handle_inbound(
            _inbound(chat_id=_CHAT_DUP, message_id=_MSG_FIRST)
        )

        # Act: the SAME update (same chat + tg_message_id) is delivered again.
        result = await service.handle_inbound(
            _inbound(chat_id=_CHAT_DUP, message_id=_MSG_FIRST)
        )

        # Assert: dedup short-circuits before any burst decision → None.
        assert result is None, "a re-delivered update must dedup and not re-ping"
