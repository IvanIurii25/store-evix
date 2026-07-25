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
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.telegram as telegram_client
from app.core.config import settings
from app.core.support_events import publish_support_event
from app.core.telegram import InboundMessage
from app.models.support import SupportConversation, SupportMessage
from app.repositories.catalog_repo import CatalogRepository
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
        redis: Redis | None = None,
        telegram=None,
    ) -> None:
        """Bind the service to its session, Redis client and Telegram client.

        Args:
            session: Active async session used for all reads and writes.
            redis: Shared async Redis client for publishing live events, or
                ``None`` for batch paths (retention purge) that never publish.
                Every HTTP caller passes it.
            telegram: Telegram client exposing ``send_message``; defaults to
                :mod:`app.core.telegram`. Tests may inject a stub.
        """
        self.session = session
        self.redis = redis
        self.repo = SupportRepository(session)
        self.telegram = telegram or telegram_client

    async def handle_inbound(self, inbound: InboundMessage) -> int | None:
        """Persist an inbound Telegram message and publish a live event (§).

        Finds or creates the conversation, refreshes the customer snapshot,
        de-duplicates re-delivered updates, appends the message, bumps activity,
        commits and publishes an ``"inbound"`` event.

        Args:
            inbound: The parsed inbound message.

        Returns:
            int | None: The ``conversation_id`` when this message starts a fresh
                unread burst (the conversation was new or previously read), so
                the caller should ping staff once. ``None`` for a re-delivered /
                deduped update or a follow-up while the conversation was already
                unread — debouncing to one ping per unread burst.
        """
        conv = await self.repo.get_by_tg_chat_id(inbound.chat_id)
        is_new = conv is None
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
                return None

        if inbound.text.startswith("/start"):
            return await self._handle_start(conv, is_new, inbound)

        fresh_burst = is_new or (conv.unread_count or 0) == 0

        await self.repo.add_message(
            conversation_id=conv.id,
            direction="in",
            text=inbound.text,
            tg_message_id=inbound.message_id,
        )
        await self.repo.bump_activity(conv, inbound=True)
        await self.session.commit()
        await publish_support_event(self.redis, conv.id, "inbound")
        return conv.id if fresh_burst else None

    async def _handle_start(
        self,
        conv: SupportConversation,
        is_new: bool,
        inbound: InboundMessage,
    ) -> int | None:
        """Handle a Telegram ``/start [payload]`` deep-link opening the chat (§).

        A storefront "Написать в поддержку" link opens the bot with a payload
        (``p<id>`` for a product, ``site``/empty otherwise). Telegram delivers it
        as the client's first message ``"/start <payload>"``. Instead of storing
        that raw command, resolves the payload into a human context line the
        operator sees, records it as the opening inbound message and sends an
        auto-greeting as an outbound system message. Always treated as a
        conversation-start, so staff are pinged.

        Args:
            conv: The find-or-created conversation (snapshot already refreshed).
            is_new: Whether the conversation was created by this update.
            inbound: The parsed ``/start`` message.

        Returns:
            int | None: The ``conversation_id`` to ping staff on, or ``None`` for
                a re-delivered ``/start`` already recorded.
        """
        if not is_new and await self.repo.message_exists(conv.id, inbound.message_id):
            return None

        payload = inbound.text[len("/start") :].strip()
        context, product_name = await self._resolve_start_context(payload, conv.lang)

        await self.repo.add_message(
            conversation_id=conv.id,
            direction="in",
            text=context,
            tg_message_id=inbound.message_id,
        )
        await self.repo.bump_activity(conv, inbound=True)

        greeting = self._start_greeting(product_name)
        msg = await self.repo.add_message(
            conversation_id=conv.id,
            direction="out",
            text=greeting,
        )
        try:
            tg_id = await self.telegram.send_message(conv.tg_chat_id, greeting)
            msg.tg_message_id = tg_id
            msg.delivery = "sent"
        except Exception:  # noqa: BLE001 — a failed send is a saved, badged message
            logger.exception(
                "support: failed to send /start greeting for conversation %s", conv.id
            )
            msg.delivery = "failed"

        await self.session.commit()
        await publish_support_event(self.redis, conv.id, "inbound")
        return conv.id

    async def _resolve_start_context(
        self,
        payload: str,
        lang: str | None,
    ) -> tuple[str, str | None]:
        """Resolve a ``/start`` payload into an operator context line (§).

        ``p<id>`` resolves to the product card (its name + a storefront link);
        anything else (``site`` / empty / unknown) yields a generic line.

        Args:
            payload: The stripped ``/start`` payload (``""``, ``"site"``, ``"p123"``…).
            lang: The conversation's language snapshot, or ``None``.

        Returns:
            tuple[str, str | None]: ``(context_line, product_name)`` — the product
                name is ``None`` for the generic case.
        """
        resolved_lang = lang or settings.default_lang
        if payload.startswith("p") and payload[1:].isdigit():
            card = await CatalogRepository(self.session).get_card(
                int(payload[1:]), resolved_lang
            )
            if card is not None:
                url = f"{settings.storefront_base_url}/{resolved_lang}/p/{card.slug}"
                return (f"🔗 Обращение по товару: «{card.name}» — {url}", card.name)
        return ("🔗 Обращение с сайта", None)

    def _start_greeting(self, product_name: str | None) -> str:
        """Build the auto-greeting sent back to the customer on ``/start``.

        Args:
            product_name: The resolved product name, or ``None`` for the generic
                greeting.

        Returns:
            str: The greeting text.
        """
        if product_name:
            return (
                f"Здравствуйте! Вы пишете по товару «{product_name}». "
                "Напишите ваш вопрос — мы на связи."
            )
        return "Здравствуйте! Напишите ваш вопрос — мы на связи."

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

    async def purge_stale(self, retention_days: int) -> int:
        """Delete conversations inactive past the retention window.

        Batch cleanup (LP195/2024 Art.5 storage limitation): removes every
        conversation whose ``last_message_at`` is older than ``retention_days``
        (their messages cascade). Does NOT publish per-row events — this is a
        bulk sweep, not an operator action.

        Args:
            retention_days: The retention window in days; conversations inactive
                longer than this are purged.

        Returns:
            int: The number of conversations deleted.
        """
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        deleted = await self.repo.delete_stale(cutoff)
        await self.session.commit()
        logger.info("support: purged %s stale conversations", deleted)
        return deleted

    async def delete_conversation(self, conversation_id: int) -> None:
        """Hard-delete one conversation and its messages (on-request erasure).

        The on-request erasure path (LP195/2024 Art.17) for Telegram support
        data, which is not account-linked. Publishes a ``"status"`` event so
        other operators' live inboxes refetch and drop the removed row.

        Args:
            conversation_id: The conversation to erase.

        Raises:
            ConversationNotFoundError: If the conversation does not exist.
        """
        deleted = await self.repo.delete_conversation(conversation_id)
        if not deleted:
            raise ConversationNotFoundError(f"Conversation {conversation_id} not found")
        await self.session.commit()
        if self.redis is not None:
            await publish_support_event(self.redis, conversation_id, "status")

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
