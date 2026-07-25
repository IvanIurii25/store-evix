"""Integration tests for linking a support conversation to an order.

An operator can attach an order to a conversation for context — by number over
the admin API (``POST/DELETE /conversations/{id}/link``), or automatically when
the customer opens the bot via an ``/start o<number>`` storefront deep-link. The
linked order surfaces on the thread read (``ThreadOut.linked_order``).

These drive the real :class:`SupportService` against the shared transactional
session + isolated Redis, and the admin router over HTTP (staff + guest). An
order is seeded with :class:`OrderFactory` (all NOT-NULL columns defaulted) so a
persisted order with a known ``number`` exists to link by.
"""

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.telegram import InboundMessage
from app.models.order import Order
from app.models.support import SupportMessage
from app.repositories.support_repo import SupportRepository
from app.services.support_service import (
    ConversationNotFoundError,
    OrderNotFoundError,
    SupportService,
)
from tests.factories import OrderFactory, persist

pytestmark = pytest.mark.asyncio

_BASE: str = "/api/v1/admin/support/conversations"

# A known order number to seed + link by (overrides the factory sequence so the
# assert on the /start context line is deterministic).
_ORDER_NUMBER: str = "20260725-000042"
# An order number that is never seeded (the unknown-order resolution path).
_MISSING_ORDER_NUMBER: str = "20260725-999999"
# A conversation-id that is never created (the missing-conversation path).
_MISSING_CONV_ID: int = 999_999
# Telegram chat ids per deep-link scenario (kept small for readable asserts).
_CHAT_ORDER: int = 5301
_CHAT_MISSING_ORDER: int = 5302
# Telegram message id for the opening /start command.
_MSG_START: int = 30
# Conversation language snapshot.
_LANG: str = "ru"
# The generic operator context line for a non-resolved deep-link.
_GENERIC_CONTEXT: str = "Обращение с сайта"


async def _seed_order(session: AsyncSession, *, number: str = _ORDER_NUMBER) -> Order:
    """Persist a standalone order (no line items) with a known number.

    Uses :class:`OrderFactory`, which defaults every NOT-NULL column
    (``email``/``phone``/``status``/``payment_status``/``subtotal``/``total``/
    ``delivery_type``); only the ``number`` is pinned so the link asserts are
    deterministic. Order items are unnecessary for the link feature.

    Args:
        session: The transactional test session.
        number: The human-readable order number to link by.

    Returns:
        Order: The persisted order.
    """
    return await persist(session, OrderFactory(number=number))


async def _seed_conversation(
    session: AsyncSession,
    *,
    tg_chat_id: int,
) -> object:
    """Persist a bare conversation directly (arrange helper), bypassing the webhook."""
    conv = await SupportRepository(session).create_conversation(
        tg_chat_id=tg_chat_id,
        customer_name="Ana",
        customer_username="ana",
        lang=_LANG,
    )
    await session.flush()
    return conv


def _start_inbound(*, chat_id: int, payload: str) -> InboundMessage:
    """Return a parsed ``/start o<payload>`` inbound for the given chat.

    Args:
        chat_id: The Telegram chat id opening the bot.
        payload: The deep-link payload appended after ``/start``.

    Returns:
        InboundMessage: The parsed opening message.
    """
    return InboundMessage(
        chat_id=chat_id,
        message_id=_MSG_START,
        text=f"/start {payload}",
        customer_name="Ana",
        customer_username="ana",
        lang=_LANG,
    )


async def _messages(
    session: AsyncSession, conversation_id: int
) -> list[SupportMessage]:
    """Return every stored message for a conversation, oldest first."""
    return list(
        (
            await session.execute(
                select(SupportMessage)
                .where(SupportMessage.conversation_id == conversation_id)
                .order_by(SupportMessage.id)
            )
        )
        .scalars()
        .all()
    )


class SupportServiceOrderLinkTest:
    """``link_order`` / ``unlink_order`` / ``get_linked_order`` domain behavior."""

    async def test_link_order_sets_link_and_get_returns_order(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a seeded order and an existing conversation to link it to.
        order = await _seed_order(db_session)
        conv = await _seed_conversation(db_session, tg_chat_id=_CHAT_ORDER)
        service = SupportService(db_session, redis_client)

        # Act: link the conversation to the order by its number.
        returned = await service.link_order(conv.id, order.number)

        # Assert: the link is set to the order's id and the order is returned.
        assert returned.id == order.id, "link_order returns the resolved order"
        assert conv.linked_order_id == order.id, (
            "the conversation's linked_order_id is set to the order id"
        )
        linked = await service.get_linked_order(conv)
        assert linked is not None, "get_linked_order resolves the linked order"
        assert linked.id == order.id, "get_linked_order returns the same order"

    async def test_link_order_unknown_number_raises_order_not_found(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a conversation but no order matching the number.
        conv = await _seed_conversation(db_session, tg_chat_id=_CHAT_ORDER)
        service = SupportService(db_session, redis_client)

        # Act / Assert: an unknown order number is a domain OrderNotFoundError.
        with pytest.raises(OrderNotFoundError):
            await service.link_order(conv.id, _MISSING_ORDER_NUMBER)

    async def test_link_order_missing_conversation_raises_not_found(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a seeded order but no conversation with the target id.
        order = await _seed_order(db_session)
        service = SupportService(db_session, redis_client)

        # Act / Assert: a missing conversation is a ConversationNotFoundError.
        with pytest.raises(ConversationNotFoundError):
            await service.link_order(_MISSING_CONV_ID, order.number)

    async def test_unlink_order_clears_the_link(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a conversation already linked to an order.
        order = await _seed_order(db_session)
        conv = await _seed_conversation(db_session, tg_chat_id=_CHAT_ORDER)
        service = SupportService(db_session, redis_client)
        await service.link_order(conv.id, order.number)

        # Act: unlink it.
        await service.unlink_order(conv.id)

        # Assert: the link is cleared → get_linked_order is None.
        assert conv.linked_order_id is None, "unlink clears linked_order_id"
        assert await service.get_linked_order(conv) is None, (
            "get_linked_order returns None after unlink"
        )

    async def test_unlink_order_missing_conversation_raises_not_found(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: no conversation with the target id.
        service = SupportService(db_session, redis_client)

        # Act / Assert: unlinking a missing conversation is a ConversationNotFoundError.
        with pytest.raises(ConversationNotFoundError):
            await service.unlink_order(_MISSING_CONV_ID)

    async def test_get_linked_order_returns_none_when_unlinked(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a conversation that was never linked to an order.
        conv = await _seed_conversation(db_session, tg_chat_id=_CHAT_ORDER)
        service = SupportService(db_session, redis_client)

        # Act / Assert: an unlinked conversation resolves to no order.
        assert await service.get_linked_order(conv) is None, (
            "an unlinked conversation has no linked order"
        )


class SupportServiceStartOrderTest:
    """``/start o<number>`` deep-link resolves + links (or falls back generic)."""

    async def test_existing_order_links_and_names_order_in_context(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a seeded order and a fresh chat opening on its deep-link.
        order = await _seed_order(db_session)
        service = SupportService(db_session, redis_client)

        # Act: ingest the opening "/start o<number>".
        outcome = await service.handle_inbound(
            _start_inbound(chat_id=_CHAT_ORDER, payload=f"o{order.number}")
        )
        conv_id, snippet = outcome.conversation_id, outcome.notify_snippet

        # Assert: a conversation is created, linked to the order, and its context
        # inbound names the order number.
        assert conv_id is not None, "opening /start starts a conversation → returns id"
        # The staff-ping snippet is the friendly order context, not the raw command.
        assert f"заказу №{order.number}" in snippet, "snippet names the order number"
        conv = await SupportRepository(db_session).get_conversation(conv_id)
        assert conv.linked_order_id == order.id, (
            "the /start o<number> deep-link links the conversation to the order"
        )
        context = (await _messages(db_session, conv_id))[0]
        assert context.direction == "in", "the context line is the opening inbound"
        assert f"№{order.number}" in context.text, (
            "the context must name the order number"
        )

    async def test_unknown_order_number_falls_back_to_generic(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: an o<number> pointing at an order that was never seeded.
        service = SupportService(db_session, redis_client)

        # Act: ingest "/start o<missing-number>".
        outcome = await service.handle_inbound(
            _start_inbound(
                chat_id=_CHAT_MISSING_ORDER, payload=f"o{_MISSING_ORDER_NUMBER}"
            )
        )
        conv_id = outcome.conversation_id

        # Assert: an unresolved order resolves like the generic "site" case — no link.
        conv = await SupportRepository(db_session).get_conversation(conv_id)
        assert conv.linked_order_id is None, (
            "an unknown order number leaves the conversation unlinked"
        )
        context = (await _messages(db_session, conv_id))[0]
        assert _GENERIC_CONTEXT in context.text, (
            "missing order → the generic 'site' context line"
        )


class AdminOrderLinkApiTest:
    """``POST/DELETE /conversations/{id}/link`` + ``linked_order`` on the detail read."""

    async def test_link_returns_200_with_order_summary(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: a seeded order + a conversation to link it to.
        order = await _seed_order(db_session)
        conv = await _seed_conversation(db_session, tg_chat_id=_CHAT_ORDER)

        # Act: link by order number over the staff API.
        resp = await client.post(
            f"{_BASE}/{conv.id}/link",
            json={"order_number": order.number},
        )

        # Assert: 200 with the LinkedOrderOut summary of the resolved order.
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["number"] == order.number, "summary carries the order number"
        assert set(body) == {
            "number",
            "status",
            "payment_status",
            "total",
            "created_at",
        }, "LinkedOrderOut exposes only the summary fields"

    async def test_link_unknown_order_number_404(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: a conversation but no order matching the number.
        conv = await _seed_conversation(db_session, tg_chat_id=_CHAT_ORDER)

        # Act: link to an order number that does not exist.
        resp = await client.post(
            f"{_BASE}/{conv.id}/link",
            json={"order_number": _MISSING_ORDER_NUMBER},
        )

        # Assert: mapped to the order-not-found 404.
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "order_not_found"

    async def test_link_unknown_conversation_404(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: a seeded order but a missing conversation id.
        order = await _seed_order(db_session)

        # Act: link a conversation that does not exist.
        resp = await client.post(
            f"{_BASE}/{_MISSING_CONV_ID}/link",
            json={"order_number": order.number},
        )

        # Assert: mapped to the conversation 404.
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_link_guest_guard(
        self,
        guest_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: an order + a conversation an unauthenticated caller must not link.
        order = await _seed_order(db_session)
        conv = await _seed_conversation(db_session, tg_chat_id=_CHAT_ORDER)

        # Act: an unauthenticated caller attempts the link.
        resp = await guest_client.post(
            f"{_BASE}/{conv.id}/link",
            json={"order_number": order.number},
        )

        # Assert: the current_staff guard blocks it (401/403).
        assert resp.status_code in (401, 403), resp.text

    async def test_unlink_returns_204(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: a conversation already linked to an order.
        order = await _seed_order(db_session)
        conv = await _seed_conversation(db_session, tg_chat_id=_CHAT_ORDER)
        await client.post(
            f"{_BASE}/{conv.id}/link", json={"order_number": order.number}
        )

        # Act: clear the link.
        resp = await client.delete(f"{_BASE}/{conv.id}/link")

        # Assert: 204 No Content.
        assert resp.status_code == 204, resp.text

    async def test_unlink_unknown_conversation_404(self, client: AsyncClient) -> None:
        # Act: unlink a conversation that does not exist.
        resp = await client.delete(f"{_BASE}/{_MISSING_CONV_ID}/link")

        # Assert: mapped to the conversation 404.
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_detail_embeds_linked_order_after_linking(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: a conversation linked to a seeded order.
        order = await _seed_order(db_session)
        conv = await _seed_conversation(db_session, tg_chat_id=_CHAT_ORDER)
        await client.post(
            f"{_BASE}/{conv.id}/link", json={"order_number": order.number}
        )

        # Act: read the conversation detail.
        resp = await client.get(f"{_BASE}/{conv.id}")

        # Assert: the response embeds the linked order's summary.
        assert resp.status_code == 200, resp.text
        linked = resp.json()["linked_order"]
        assert linked is not None, "detail embeds the linked order after linking"
        assert linked["number"] == order.number, "embedded summary is the linked order"

    async def test_detail_linked_order_null_after_unlink(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: a conversation linked then unlinked from an order.
        order = await _seed_order(db_session)
        conv = await _seed_conversation(db_session, tg_chat_id=_CHAT_ORDER)
        await client.post(
            f"{_BASE}/{conv.id}/link", json={"order_number": order.number}
        )
        await client.delete(f"{_BASE}/{conv.id}/link")

        # Act: read the conversation detail after unlinking.
        resp = await client.get(f"{_BASE}/{conv.id}")

        # Assert: linked_order is null once the link is cleared.
        assert resp.status_code == 200, resp.text
        assert resp.json()["linked_order"] is None, (
            "detail carries a null linked_order after unlink"
        )
