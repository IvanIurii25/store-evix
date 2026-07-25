"""Async data-access layer for the Telegram support helpdesk.

Only SQL / query construction lives here — no business rules (those belong to
the support service). All statements are async SQLAlchemy 2.0 bound to one
session.

Conversations are the inbox rows (updated in place: status / unread_count /
last_message_at); messages are append-only thread rows. Inbound Telegram
updates can be re-delivered, so :meth:`SupportRepository.message_exists` gives
the service a de-duplication check keyed on ``(conversation_id, tg_message_id)``.
"""

from datetime import datetime

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.support import SupportConversation, SupportMessage


class SupportRepository:
    """Read/write queries for the support inbox, bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used by every query method.

        Args:
            session: Active async session (request-scoped or test-scoped).
        """
        self.session = session

    @staticmethod
    def _apply_status(stmt: Select, status: str | None) -> Select:
        """Attach the optional exact ``status`` filter to a statement.

        Args:
            stmt: The base ``SELECT`` (over ``SupportConversation`` or a count).
            status: Exact conversation status to filter by, or ``None``.

        Returns:
            Select: The statement with the requested predicate applied.
        """
        if status is not None:
            stmt = stmt.where(SupportConversation.status == status)
        return stmt

    async def get_by_tg_chat_id(
        self,
        tg_chat_id: int,
    ) -> SupportConversation | None:
        """Return the conversation for a Telegram chat id, or ``None``.

        Args:
            tg_chat_id: The Telegram chat id (unique per conversation).

        Returns:
            SupportConversation | None: The matching conversation, if any.
        """
        stmt = select(SupportConversation).where(
            SupportConversation.tg_chat_id == tg_chat_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create_conversation(
        self,
        *,
        tg_chat_id: int,
        customer_name: str | None,
        customer_username: str | None,
        lang: str | None,
    ) -> SupportConversation:
        """Insert a new conversation and return it flushed.

        Status, unread_count and timestamps are left to their DB defaults.

        Args:
            tg_chat_id: The Telegram chat id (unique per conversation).
            customer_name: First+last name snapshot from Telegram, or ``None``.
            customer_username: ``@username`` snapshot, or ``None``.
            lang: Telegram language code, or ``None``.

        Returns:
            SupportConversation: The persisted conversation with its id.
        """
        conversation = SupportConversation(
            tg_chat_id=tg_chat_id,
            customer_name=customer_name,
            customer_username=customer_username,
            lang=lang,
        )
        self.session.add(conversation)
        await self.session.flush()
        return conversation

    async def add_message(
        self,
        *,
        conversation_id: int,
        direction: str,
        text: str,
        tg_message_id: int | None = None,
        sender_staff_id: int | None = None,
        delivery: str | None = None,
    ) -> SupportMessage:
        """Insert one append-only message and return it flushed.

        Args:
            conversation_id: The owning conversation's primary key.
            direction: ``"in"`` (from customer) or ``"out"`` (from operator).
            text: Message body.
            tg_message_id: Telegram message id, or ``None``.
            sender_staff_id: Operator who sent an outbound message, or ``None``.
            delivery: Outbound send result, or ``None``.

        Returns:
            SupportMessage: The persisted message with its id.
        """
        message = SupportMessage(
            conversation_id=conversation_id,
            direction=direction,
            text=text,
            tg_message_id=tg_message_id,
            sender_staff_id=sender_staff_id,
            delivery=delivery,
        )
        self.session.add(message)
        await self.session.flush()
        return message

    async def message_exists(
        self,
        conversation_id: int,
        tg_message_id: int,
    ) -> bool:
        """Return whether a message with that Telegram id already exists.

        Used for inbound de-duplication (Telegram re-delivers updates).

        Args:
            conversation_id: The owning conversation's primary key.
            tg_message_id: The Telegram message id to look for.

        Returns:
            bool: ``True`` if such a message already exists.
        """
        stmt = (
            select(SupportMessage.id)
            .where(
                SupportMessage.conversation_id == conversation_id,
                SupportMessage.tg_message_id == tg_message_id,
            )
            .limit(1)
        )
        return (await self.session.scalar(stmt)) is not None

    async def count_conversations(self, *, status: str | None) -> int:
        """Return the total number of conversations matching the filter.

        Args:
            status: Exact conversation status to filter by, or ``None``.

        Returns:
            int: Total matching row count (across all pages).
        """
        base = select(func.count()).select_from(SupportConversation)
        stmt = self._apply_status(base, status)
        return int((await self.session.scalar(stmt)) or 0)

    async def list_conversations(
        self,
        *,
        status: str | None,
        offset: int,
        limit: int,
    ) -> list[SupportConversation]:
        """Return one page of conversations, newest activity first.

        Args:
            status: Exact conversation status to filter by, or ``None``.
            offset: Number of rows to skip (``(page - 1) * page_size``).
            limit: Page size.

        Returns:
            list[SupportConversation]: The page of conversations, ordered by
            ``last_message_at`` descending (the inbox order).
        """
        stmt = self._apply_status(select(SupportConversation), status)
        stmt = stmt.order_by(SupportConversation.last_message_at.desc())
        stmt = stmt.offset(offset).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_conversation(
        self,
        conversation_id: int,
    ) -> SupportConversation | None:
        """Return a conversation by primary key, or ``None``.

        Args:
            conversation_id: The conversation's primary key.

        Returns:
            SupportConversation | None: The matching conversation, if any.
        """
        stmt = select(SupportConversation).where(
            SupportConversation.id == conversation_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_thread(
        self,
        conversation_id: int,
        *,
        offset: int,
        limit: int,
    ) -> list[SupportMessage]:
        """Return one page of a conversation's messages, chronological.

        Args:
            conversation_id: The owning conversation's primary key.
            offset: Number of rows to skip (``(page - 1) * page_size``).
            limit: Page size.

        Returns:
            list[SupportMessage]: The page of messages, ordered by
            ``created_at`` ascending (the thread order).
        """
        stmt = (
            select(SupportMessage)
            .where(SupportMessage.conversation_id == conversation_id)
            .order_by(SupportMessage.created_at.asc())
            .offset(offset)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_thread(self, conversation_id: int) -> int:
        """Return the total number of messages in a conversation.

        Args:
            conversation_id: The owning conversation's primary key.

        Returns:
            int: Total message count (across all pages).
        """
        stmt = (
            select(func.count())
            .select_from(SupportMessage)
            .where(SupportMessage.conversation_id == conversation_id)
        )
        return int((await self.session.scalar(stmt)) or 0)

    async def set_status(
        self,
        conversation_id: int,
        status: str,
    ) -> SupportConversation | None:
        """Set a conversation's status in place and return it flushed.

        Args:
            conversation_id: The conversation's primary key.
            status: The new status value.

        Returns:
            SupportConversation | None: The updated conversation, or ``None`` if
            it does not exist.
        """
        conversation = await self.get_conversation(conversation_id)
        if conversation is None:
            return None
        conversation.status = status
        await self.session.flush()
        return conversation

    async def mark_read(self, conversation_id: int) -> None:
        """Reset a conversation's unread counter (operator opened it).

        Args:
            conversation_id: The conversation's primary key.
        """
        stmt = (
            update(SupportConversation)
            .where(SupportConversation.id == conversation_id)
            .values(unread_count=0)
        )
        await self.session.execute(stmt)

    async def delete_stale(self, cutoff: datetime) -> int:
        """Delete every conversation last active before ``cutoff``.

        The ``support_message.conversation_id`` FK is ``ON DELETE CASCADE``, so
        each conversation's messages are removed with it.

        Args:
            cutoff: Conversations whose ``last_message_at`` is strictly before
                this instant are deleted (the retention window boundary).

        Returns:
            int: The number of conversations deleted.
        """
        stmt = delete(SupportConversation).where(
            SupportConversation.last_message_at < cutoff
        )
        result = await self.session.execute(stmt)
        return result.rowcount

    async def delete_conversation(self, conversation_id: int) -> bool:
        """Delete one conversation by primary key (messages cascade).

        Args:
            conversation_id: The conversation's primary key.

        Returns:
            bool: ``True`` if a conversation was deleted, ``False`` if none
            matched the id.
        """
        stmt = delete(SupportConversation).where(
            SupportConversation.id == conversation_id
        )
        result = await self.session.execute(stmt)
        return result.rowcount > 0

    async def bump_activity(
        self,
        conversation: SupportConversation,
        *,
        inbound: bool,
    ) -> None:
        """Bump a loaded conversation's activity on a new message.

        Sets ``last_message_at`` to now and, for inbound messages, increments
        ``unread_count`` by one. Operates on an already-loaded conversation; the
        caller controls the transaction.

        Args:
            conversation: The conversation the message belongs to.
            inbound: Whether the triggering message is from the customer.
        """
        conversation.last_message_at = func.now()
        if inbound:
            conversation.unread_count = conversation.unread_count + 1
