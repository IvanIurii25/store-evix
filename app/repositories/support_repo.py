"""Async data-access layer for the Telegram support helpdesk.

Only SQL / query construction lives here — no business rules (those belong to
the support service). All statements are async SQLAlchemy 2.0 bound to one
session.

Conversations are the inbox rows (updated in place: status / unread_count /
last_message_at); messages are append-only thread rows. Inbound Telegram
updates can be re-delivered, so :meth:`SupportRepository.message_exists` gives
the service a de-duplication check keyed on ``(conversation_id, tg_message_id)``.
"""

from datetime import date, datetime

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.support import (
    SupportCannedResponse,
    SupportConversation,
    SupportMessage,
)


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
        attachment_kind: str | None = None,
        attachment_name: str | None = None,
    ) -> SupportMessage:
        """Insert one append-only message and return it flushed.

        Args:
            conversation_id: The owning conversation's primary key.
            direction: ``"in"`` (from customer) or ``"out"`` (from operator).
            text: Message body.
            tg_message_id: Telegram message id, or ``None``.
            sender_staff_id: Operator who sent an outbound message, or ``None``.
            delivery: Outbound send result, or ``None``.
            attachment_kind: ``"photo"``/``"document"`` for an inbound message
                carrying a file, or ``None`` for a plain-text message. The object
                key (``attachment_key``) is filled asynchronously by the fetch
                task, so it is left NULL here.
            attachment_name: Original document filename, or ``None``.

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
            attachment_kind=attachment_kind,
            attachment_name=attachment_name,
        )
        self.session.add(message)
        await self.session.flush()
        return message

    async def get_message(self, message_id: int) -> SupportMessage | None:
        """Return a message by primary key, or ``None``.

        Used by the staff attachment proxy to resolve a message's stored object
        key before streaming it.

        Args:
            message_id: The message's primary key.

        Returns:
            SupportMessage | None: The matching message, if any.
        """
        stmt = select(SupportMessage).where(SupportMessage.id == message_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

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

    async def metrics_summary(self, cutoff: datetime) -> dict:
        """Return conversation counts for the support-metrics overview.

        Args:
            cutoff: Only conversations created at/after this instant count toward
                ``new_in_period`` (the window boundary).

        Returns:
            dict: ``{"total", "new_in_period", "open", "pending", "closed"}`` —
            ``total`` and ``new_in_period`` are period-scoped counts; the status
            keys are the *current* per-status conversation counts (missing
            statuses default to ``0``).
        """
        total = (
            await self.session.scalar(
                select(func.count()).select_from(SupportConversation)
            )
        ) or 0
        new_in_period = (
            await self.session.scalar(
                select(func.count())
                .select_from(SupportConversation)
                .where(SupportConversation.created_at >= cutoff)
            )
        ) or 0
        status_stmt = select(
            SupportConversation.status, func.count().label("n")
        ).group_by(SupportConversation.status)
        by_status = {
            row.status: int(row.n) for row in (await self.session.execute(status_stmt))
        }
        return {
            "total": int(total),
            "new_in_period": int(new_in_period),
            "open": by_status.get("open", 0),
            "pending": by_status.get("pending", 0),
            "closed": by_status.get("closed", 0),
        }

    async def unanswered_count(self) -> int:
        """Return how many non-closed conversations await an operator reply.

        A conversation counts when it is not ``closed`` and its most recent
        message (highest ``created_at``) is inbound (``direction = 'in'``) — i.e.
        the customer spoke last and nobody has replied. Conversations with no
        messages do not count.

        Returns:
            int: The number of conversations whose last message is inbound.
        """
        latest = (
            select(
                SupportMessage.conversation_id.label("conversation_id"),
                func.max(SupportMessage.created_at).label("last_at"),
            )
            .group_by(SupportMessage.conversation_id)
            .subquery()
        )
        last_msg = (
            select(SupportMessage.direction)
            .where(
                SupportMessage.conversation_id == latest.c.conversation_id,
                SupportMessage.created_at == latest.c.last_at,
            )
            .order_by(SupportMessage.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        stmt = (
            select(func.count())
            .select_from(SupportConversation)
            .join(latest, latest.c.conversation_id == SupportConversation.id)
            .where(
                SupportConversation.status != "closed",
                last_msg == "in",
            )
        )
        return int((await self.session.scalar(stmt)) or 0)

    async def avg_first_response_seconds(self, cutoff: datetime) -> float | None:
        """Return the average first-response time (seconds) over the window.

        Over conversations *created* at/after ``cutoff`` that received a real
        operator reply: per conversation take the first inbound message time and
        the first outbound message time sent by an operator (``sender_staff_id``
        set), keep only conversations where both exist and the reply is not
        before the question, and average the differences.

        Args:
            cutoff: Only conversations created at/after this instant are counted.

        Returns:
            float | None: The average first-response time in seconds, or ``None``
            when no qualifying conversation exists in the window.
        """
        first_times = (
            select(
                SupportMessage.conversation_id.label("conversation_id"),
                func.min(SupportMessage.created_at)
                .filter(SupportMessage.direction == "in")
                .label("t_in"),
                func.min(SupportMessage.created_at)
                .filter(
                    SupportMessage.direction == "out",
                    SupportMessage.sender_staff_id.is_not(None),
                )
                .label("t_out"),
            )
            .group_by(SupportMessage.conversation_id)
            .subquery()
        )
        stmt = (
            select(
                func.avg(
                    func.extract("epoch", first_times.c.t_out - first_times.c.t_in)
                )
            )
            .select_from(SupportConversation)
            .join(
                first_times,
                first_times.c.conversation_id == SupportConversation.id,
            )
            .where(
                SupportConversation.created_at >= cutoff,
                first_times.c.t_in.is_not(None),
                first_times.c.t_out.is_not(None),
                first_times.c.t_out >= first_times.c.t_in,
            )
        )
        avg = await self.session.scalar(stmt)
        return float(avg) if avg is not None else None

    async def new_conversations_series(
        self,
        cutoff: datetime,
    ) -> list[tuple[date, int]]:
        """Return new conversations per day since ``cutoff``, ascending.

        Args:
            cutoff: Only conversations created at/after this instant are counted.

        Returns:
            list[tuple[date, int]]: ``(day, count)`` pairs, one per day that had
            at least one new conversation, ordered by day.
        """
        day = func.date_trunc("day", SupportConversation.created_at).label("day")
        stmt = (
            select(day, func.count().label("count"))
            .where(SupportConversation.created_at >= cutoff)
            .group_by(day)
            .order_by(day)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(row.day.date(), int(row.count)) for row in rows]

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

    async def set_linked_order(
        self,
        conversation_id: int,
        order_id: int | None,
    ) -> SupportConversation | None:
        """Set (or clear) a conversation's linked order and return it flushed.

        Args:
            conversation_id: The conversation's primary key.
            order_id: The order to link, or ``None`` to clear the link.

        Returns:
            SupportConversation | None: The updated conversation, or ``None`` if
            it does not exist.
        """
        conversation = await self.get_conversation(conversation_id)
        if conversation is None:
            return None
        conversation.linked_order_id = order_id
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

    async def attachment_keys_for_conversation(
        self,
        conversation_id: int,
    ) -> list[str]:
        """Return the stored object keys of a conversation's attachments.

        Collected before erasure so the caller can remove the files from object
        storage after the rows are deleted (the FK cascade only drops the DB
        rows, never the stored bytes).

        Args:
            conversation_id: The owning conversation's primary key.

        Returns:
            list[str]: Every non-NULL ``attachment_key`` of the conversation's
            messages (empty when it has no stored attachments).
        """
        stmt = select(SupportMessage.attachment_key).where(
            SupportMessage.conversation_id == conversation_id,
            SupportMessage.attachment_key.is_not(None),
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def attachment_keys_stale(self, cutoff: datetime) -> list[str]:
        """Return the stored object keys of stale conversations' attachments.

        Mirrors :meth:`delete_stale`'s selection (conversations whose
        ``last_message_at`` is strictly before ``cutoff``) so the keys match the
        rows about to be purged. Collected before the delete so the caller can
        remove the files from object storage afterwards.

        Args:
            cutoff: Conversations whose ``last_message_at`` is strictly before
                this instant are considered stale (the retention boundary).

        Returns:
            list[str]: Every non-NULL ``attachment_key`` of messages belonging to
            stale conversations (empty when none have stored attachments).
        """
        stale_ids = select(SupportConversation.id).where(
            SupportConversation.last_message_at < cutoff
        )
        stmt = select(SupportMessage.attachment_key).where(
            SupportMessage.conversation_id.in_(stale_ids),
            SupportMessage.attachment_key.is_not(None),
        )
        return list((await self.session.execute(stmt)).scalars().all())

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


class SupportCannedRepository:
    """Read/write queries for canned reply templates, bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used by every query method.

        Args:
            session: Active async session (request-scoped or test-scoped).
        """
        self.session = session

    async def list(self, *, lang: str | None) -> list[SupportCannedResponse]:
        """Return canned responses, ordered for the picker.

        Args:
            lang: Exact language to filter by, or ``None`` for all languages.

        Returns:
            list[SupportCannedResponse]: The templates, ordered by
            ``sort_order`` then ``id``.
        """
        stmt = select(SupportCannedResponse)
        if lang is not None:
            stmt = stmt.where(SupportCannedResponse.lang == lang)
        stmt = stmt.order_by(
            SupportCannedResponse.sort_order.asc(),
            SupportCannedResponse.id.asc(),
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(self, canned_id: int) -> SupportCannedResponse | None:
        """Return a canned response by primary key, or ``None``.

        Args:
            canned_id: The template's primary key.

        Returns:
            SupportCannedResponse | None: The matching template, if any.
        """
        stmt = select(SupportCannedResponse).where(
            SupportCannedResponse.id == canned_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create(
        self,
        *,
        title: str,
        text: str,
        lang: str,
        sort_order: int,
    ) -> SupportCannedResponse:
        """Insert a new canned response and return it flushed.

        Args:
            title: Short picker label.
            text: The reply body.
            lang: The template language.
            sort_order: Manual ordering within the language.

        Returns:
            SupportCannedResponse: The persisted template with its id.
        """
        canned = SupportCannedResponse(
            title=title,
            text=text,
            lang=lang,
            sort_order=sort_order,
        )
        self.session.add(canned)
        await self.session.flush()
        return canned

    async def update(
        self,
        canned_id: int,
        *,
        title: str,
        text: str,
        lang: str,
        sort_order: int,
    ) -> SupportCannedResponse | None:
        """Update a canned response in place and return it flushed.

        Args:
            canned_id: The template's primary key.
            title: Short picker label.
            text: The reply body.
            lang: The template language.
            sort_order: Manual ordering within the language.

        Returns:
            SupportCannedResponse | None: The updated template, or ``None`` if it
            does not exist.
        """
        canned = await self.get(canned_id)
        if canned is None:
            return None
        canned.title = title
        canned.text = text
        canned.lang = lang
        canned.sort_order = sort_order
        await self.session.flush()
        return canned

    async def delete(self, canned_id: int) -> bool:
        """Delete one canned response by primary key.

        Args:
            canned_id: The template's primary key.

        Returns:
            bool: ``True`` if a template was deleted, ``False`` if none matched.
        """
        stmt = delete(SupportCannedResponse).where(
            SupportCannedResponse.id == canned_id
        )
        result = await self.session.execute(stmt)
        return result.rowcount > 0
