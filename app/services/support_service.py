"""Telegram support helpdesk business logic (support module).

Sits between the webhook / admin routers and :class:`SupportRepository`. Owns
the domain rules:

* Inbound: find-or-create the conversation for a Telegram chat, refresh its
  customer snapshot, de-duplicate re-delivered updates, append the message and
  bump inbox activity — then publish an ``"inbound"`` live event.
* Reply: append the outbound message first (so it is never lost), attempt the
  Telegram send, record ``sent``/``failed`` delivery (a send failure is a saved
  message with a failed badge, not an error the operator sees) and publish an
  ``"outbound"`` event.
* Status changes publish a ``"status"`` event.

Telegram sending is injected (:mod:`app.core.telegram` by default) so tests can
pass a stub exposing ``send_message``. The service commits its own writes (the
session dependency does not auto-commit). No HTTP knowledge: the router maps the
raised exceptions.
"""

import logging

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.telegram as telegram_client
from app.core.support_events import publish_support_event
from app.core.telegram import InboundMessage
from app.models.support import SupportConversation, SupportMessage
from app.repositories.support_repo import SupportRepository

logger = logging.getLogger(__name__)


class SupportError(Exception):
    """Base class for support domain errors (mapped to HTTP by the router)."""

    code: str = "support_error"


class ConversationNotFoundError(SupportError):
    """The referenced conversation does not exist."""

    code = "not_found"


class SupportService:
    """Support-inbox operations bound to one session and Redis client."""

    def __init__(
        self,
        session: AsyncSession,
        redis: Redis,
        telegram=None,
    ) -> None:
        """Bind the service to its session, Redis client and Telegram client.

        Args:
            session: Active async session used for all reads and writes.
            redis: Shared async Redis client for publishing live events.
            telegram: Telegram client exposing ``send_message``; defaults to
                :mod:`app.core.telegram`. Tests may inject a stub.
        """
        self.session = session
        self.redis = redis
        self.repo = SupportRepository(session)
        self.telegram = telegram or telegram_client

    async def handle_inbound(self, inbound: InboundMessage) -> None:
        """Persist an inbound Telegram message and publish a live event (§).

        Finds or creates the conversation, refreshes the customer snapshot,
        de-duplicates re-delivered updates, appends the message, bumps activity,
        commits and publishes an ``"inbound"`` event.

        Args:
            inbound: The parsed inbound message.
        """
        conv = await self.repo.get_by_tg_chat_id(inbound.chat_id)
        if conv is None:
            conv = await self.repo.create_conversation(
                tg_chat_id=inbound.chat_id,
                customer_name=inbound.customer_name,
                customer_username=inbound.customer_username,
                lang=inbound.lang,
            )
        else:
            conv.customer_name = inbound.customer_name
            conv.customer_username = inbound.customer_username
            conv.lang = inbound.lang

        if await self.repo.message_exists(conv.id, inbound.message_id):
            return

        await self.repo.add_message(
            conversation_id=conv.id,
            direction="in",
            text=inbound.text,
            tg_message_id=inbound.message_id,
        )
        await self.repo.bump_activity(conv, inbound=True)
        await self.session.commit()
        await publish_support_event(self.redis, conv.id, "inbound")

    async def reply(
        self,
        conversation_id: int,
        staff_id: int,
        text: str,
    ) -> SupportMessage:
        """Send an operator reply, recording the delivery result (§).

        Appends the outbound message first, attempts the Telegram send and
        records ``sent``/``failed`` delivery (a send failure is saved, not
        raised), marks the conversation read, commits and publishes an
        ``"outbound"`` event.

        Args:
            conversation_id: The conversation to reply in.
            staff_id: The operator sending the reply.
            text: The reply body.

        Returns:
            SupportMessage: The persisted outbound message.

        Raises:
            ConversationNotFoundError: If the conversation does not exist.
        """
        conv = await self.repo.get_conversation(conversation_id)
        if conv is None:
            raise ConversationNotFoundError(f"Conversation {conversation_id} not found")

        msg = await self.repo.add_message(
            conversation_id=conv.id,
            direction="out",
            text=text,
            sender_staff_id=staff_id,
        )
        try:
            tg_message_id = await self.telegram.send_message(conv.tg_chat_id, text)
            msg.tg_message_id = tg_message_id
            msg.delivery = "sent"
        except Exception:  # noqa: BLE001 — a failed send is a saved, badged message
            logger.exception(
                "support: failed to send reply for conversation %s", conv.id
            )
            msg.delivery = "failed"

        await self.repo.bump_activity(conv, inbound=False)
        await self.repo.mark_read(conv.id)
        await self.session.commit()
        await publish_support_event(self.redis, conv.id, "outbound")
        return msg

    async def set_status(
        self,
        conversation_id: int,
        status: str,
    ) -> SupportConversation:
        """Change a conversation's status and publish a live event (§).

        Args:
            conversation_id: The conversation to update.
            status: The new status value.

        Returns:
            SupportConversation: The updated conversation.

        Raises:
            ConversationNotFoundError: If the conversation does not exist.
        """
        conv = await self.repo.set_status(conversation_id, status)
        if conv is None:
            raise ConversationNotFoundError(f"Conversation {conversation_id} not found")
        await self.session.commit()
        await publish_support_event(self.redis, conv.id, "status")
        return conv

    async def list_conversations(
        self,
        *,
        status: str | None,
        offset: int,
        limit: int,
    ) -> list[SupportConversation]:
        """Return one page of inbox conversations.

        Args:
            status: Exact conversation status to filter by, or ``None``.
            offset: Number of rows to skip.
            limit: Page size.

        Returns:
            list[SupportConversation]: The requested page.
        """
        return await self.repo.list_conversations(
            status=status, offset=offset, limit=limit
        )

    async def count_conversations(self, *, status: str | None) -> int:
        """Return the total conversation count for the filter.

        Args:
            status: Exact conversation status to filter by, or ``None``.

        Returns:
            int: The matching row count.
        """
        return await self.repo.count_conversations(status=status)

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
        return await self.repo.get_conversation(conversation_id)

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
            offset: Number of rows to skip.
            limit: Page size.

        Returns:
            list[SupportMessage]: The requested page of messages.
        """
        return await self.repo.get_thread(conversation_id, offset=offset, limit=limit)

    async def count_thread(self, conversation_id: int) -> int:
        """Return the total message count in a conversation.

        Args:
            conversation_id: The owning conversation's primary key.

        Returns:
            int: The total message count.
        """
        return await self.repo.count_thread(conversation_id)

    async def mark_read(self, conversation_id: int) -> None:
        """Reset a conversation's unread counter (operator opened it).

        Args:
            conversation_id: The conversation's primary key.
        """
        await self.repo.mark_read(conversation_id)
        await self.session.commit()
