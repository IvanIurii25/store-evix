"""Integration tests for the Telegram ``/start <payload>`` deep-link handling.

A storefront "Написать в поддержку" link opens the bot with a payload that
Telegram delivers as the client's first message ``"/start <payload>"``. Instead
of storing that raw command, :meth:`SupportService.handle_inbound` resolves the
payload into an operator-facing context line (the opening INBOUND message) and
auto-replies with a greeting (an OUTBOUND system message):

* ``/start p<id>`` for a published product → context + greeting name the product
  and the context carries the storefront ``/<lang>/p/<slug>`` link;
* ``/start`` bare / ``/start site`` / an unknown product id → the generic
  "Обращение с сайта" context and a generic greeting;
* a re-delivered ``/start`` (same chat + ``tg_message_id``) is deduped;
* with no bot token configured (test default) the greeting send is a no-op that
  returns ``None`` — the greeting still persists as ``sent`` with no tg id.

These drive the real service against the shared session + isolated Redis, seeding
the product via the denormalized ``product_card`` read-model that ``get_card``
(the resolver's only read) actually queries.
"""

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.telegram as telegram_module
from app.core.config import settings
from app.core.telegram import InboundMessage
from app.models.support import SupportMessage
from app.services.support_service import SupportService
from tests.factories import (
    ProductCardFactory,
    ProductFactory,
    create_category,
    persist,
)

pytestmark = pytest.mark.asyncio

# Telegram chat ids per scenario (kept small for readable asserts).
_CHAT_PRODUCT: int = 4201
_CHAT_SITE: int = 4202
_CHAT_MISSING: int = 4203
_CHAT_DUP: int = 4204
_CHAT_FAIL: int = 4205
# Telegram message id for the opening /start command.
_MSG_START: int = 20
# Language of the seeded card + conversation snapshot.
_LANG: str = "ru"
# Product identity used by the seeded card and the p<id> payload.
_PRODUCT_NAME: str = "Тестовый смартфон"
_PRODUCT_SLUG: str = "test-smartfon"
# A product id that is never seeded (non-existent resolution path).
_MISSING_PRODUCT_ID: int = 999_999


def _start_inbound(*, chat_id: int, payload: str) -> InboundMessage:
    """Return a parsed ``/start [payload]`` inbound for the given chat.

    Args:
        chat_id: The Telegram chat id opening the bot.
        payload: The deep-link payload appended after ``/start`` (``""`` for a
            bare ``/start``).

    Returns:
        InboundMessage: The parsed opening message.
    """
    text = "/start" if payload == "" else f"/start {payload}"
    return InboundMessage(
        chat_id=chat_id,
        message_id=_MSG_START,
        text=text,
        customer_name="Ana",
        customer_username="ana",
        lang=_LANG,
    )


async def _seed_product_card(session: AsyncSession) -> int:
    """Seed a product + its ``product_card`` read-model row and return its id.

    ``_resolve_start_context`` resolves ``p<id>`` via ``CatalogRepository.get_card``,
    which reads only the denormalized ``product_card`` (by ``product_id`` + ``lang``),
    so a card row with a known ``name``/``slug`` is all the resolver needs.

    Args:
        session: The transactional test session.

    Returns:
        int: The seeded product's id (use it as the ``p<id>`` payload).
    """
    category = await create_category(session)
    product = await persist(session, ProductFactory(category_id=category.id))
    await persist(
        session,
        ProductCardFactory(
            product_id=product.id,
            lang=_LANG,
            name=_PRODUCT_NAME,
            slug=_PRODUCT_SLUG,
        ),
    )
    return product.id


async def _thread_texts(session: AsyncSession, conversation_id: int) -> list[str]:
    """Return every stored message text for a conversation, oldest first."""
    rows = (
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
    return [row.text for row in rows]


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


class SupportServiceStartProductTest:
    """``/start p<id>`` for a published product resolves name + link + greeting."""

    async def test_existing_product_records_context_and_greeting(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a published product (card row) and a fresh chat opening on it.
        product_id = await _seed_product_card(db_session)
        service = SupportService(db_session, redis_client)

        # Act: ingest the opening "/start p<id>".
        conv_id, snippet = await service.handle_inbound(
            _start_inbound(chat_id=_CHAT_PRODUCT, payload=f"p{product_id}")
        )

        # Assert: a fresh conversation → its id is returned (staff get pinged).
        assert conv_id is not None, "opening /start starts a conversation → returns id"
        # The staff-ping snippet is the friendly product context, not the raw command.
        assert not snippet.startswith("/start"), "snippet must not be the raw command"
        assert _PRODUCT_NAME in snippet, "snippet is the 'по товару' product context"

        messages = await _messages(db_session, conv_id)
        assert len(messages) == 2, "exactly the context inbound + greeting outbound"
        context, greeting = messages

        # The INBOUND context names the product and carries the storefront link.
        expected_url = f"{settings.storefront_base_url}/{_LANG}/p/{_PRODUCT_SLUG}"
        assert context.direction == "in", "the context line is the opening inbound"
        assert _PRODUCT_NAME in context.text, "context must name the product"
        assert expected_url in context.text, "context must carry the /ru/p/<slug> link"

        # The OUTBOUND greeting is the product-specific one.
        assert greeting.direction == "out", "the greeting is an outbound system message"
        assert _PRODUCT_NAME in greeting.text, "greeting must name the product"

    async def test_raw_start_command_is_not_stored(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a published product and a fresh chat.
        product_id = await _seed_product_card(db_session)
        service = SupportService(db_session, redis_client)

        # Act: ingest the opening "/start p<id>".
        conv_id, _snippet = await service.handle_inbound(
            _start_inbound(chat_id=_CHAT_PRODUCT, payload=f"p{product_id}")
        )

        # Assert: the raw command text is never persisted as a message.
        texts = await _thread_texts(db_session, conv_id)
        assert all(not text.startswith("/start") for text in texts), (
            "the raw /start command must not be stored as a message"
        )

    async def test_greeting_persists_sent_with_no_tg_id(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a product; the test default has no bot token → send is a no-op.
        product_id = await _seed_product_card(db_session)
        service = SupportService(db_session, redis_client)

        # Act: open the chat with a product deep-link.
        conv_id, _snippet = await service.handle_inbound(
            _start_inbound(chat_id=_CHAT_PRODUCT, payload=f"p{product_id}")
        )

        # Assert: the greeting persisted as "sent" with no Telegram id (dev no-op).
        outbound = [
            m for m in await _messages(db_session, conv_id) if m.direction == "out"
        ]
        assert len(outbound) == 1, "exactly one greeting outbound message"
        assert outbound[0].delivery == "sent", "no-op send records a sent greeting"
        assert outbound[0].tg_message_id is None, "no-op send returns no tg id"


class SupportServiceStartGenericTest:
    """Bare ``/start`` / ``site`` / unknown id fall back to the generic context."""

    # The generic operator context line for a non-product deep-link.
    _GENERIC_CONTEXT: str = "Обращение с сайта"

    @pytest.mark.parametrize("payload", ["", "site"], ids=["bare", "site"])
    async def test_non_product_payload_uses_generic_context_and_greeting(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
        payload: str,
    ) -> None:
        # Arrange: a fresh chat opening the bot with no / a site payload.
        service = SupportService(db_session, redis_client)

        # Act: ingest the bare-or-site "/start".
        conv_id, snippet = await service.handle_inbound(
            _start_inbound(chat_id=_CHAT_SITE, payload=payload)
        )

        # Assert: generic context inbound + a greeting naming no product.
        assert conv_id is not None, "opening /start returns the conversation id"
        # The staff-ping snippet is the generic context, NOT the raw "/start site".
        assert snippet == "🔗 Обращение с сайта", "snippet is the generic context line"
        context, greeting = await _messages(db_session, conv_id)
        assert self._GENERIC_CONTEXT in context.text, "generic 'site' context line"
        assert _PRODUCT_NAME not in greeting.text, "generic greeting names no product"

    async def test_unknown_product_id_falls_back_to_generic(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a p<id> pointing at a product that was never seeded.
        service = SupportService(db_session, redis_client)

        # Act: ingest "/start p<missing_id>".
        conv_id, _snippet = await service.handle_inbound(
            _start_inbound(chat_id=_CHAT_MISSING, payload=f"p{_MISSING_PRODUCT_ID}")
        )

        # Assert: an unresolved product resolves like the generic "site" case.
        context, greeting = await _messages(db_session, conv_id)
        assert self._GENERIC_CONTEXT in context.text, (
            "missing product → generic context"
        )
        assert _PRODUCT_NAME not in greeting.text, "missing product → generic greeting"


class SupportServiceStartEdgeTest:
    """Re-delivered ``/start`` dedup and greeting send-failure persistence."""

    async def test_redelivered_start_dedups_without_duplicates(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a first "/start site" establishes the conversation.
        service = SupportService(db_session, redis_client)
        conv_id, _snippet = await service.handle_inbound(
            _start_inbound(chat_id=_CHAT_DUP, payload="site")
        )
        before = await _count_messages(db_session, conv_id)

        # Act: the SAME update (same chat + tg_message_id) is delivered again.
        result_id, snippet = await service.handle_inbound(
            _start_inbound(chat_id=_CHAT_DUP, payload="site")
        )

        # Assert: the re-delivery dedups — no id, empty snippet, no extra messages.
        assert result_id is None, "a re-delivered /start must dedup and not re-ping"
        assert snippet == "", "a deduped /start carries no ping snippet"
        after = await _count_messages(db_session, conv_id)
        assert after == before, "dedup must not append duplicate messages"

    async def test_greeting_send_failure_persists_as_failed(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: force the Telegram send to raise (mirrors the reply failure path).
        async def _boom(chat_id: int, text: str) -> int:
            raise RuntimeError("telegram down")

        monkeypatch.setattr(telegram_module, "send_message", _boom)
        service = SupportService(db_session, redis_client)

        # Act: open the chat — the greeting send fails but must not raise.
        conv_id, _snippet = await service.handle_inbound(
            _start_inbound(chat_id=_CHAT_FAIL, payload="site")
        )

        # Assert: the greeting is saved with a "failed" delivery badge, call succeeds.
        assert conv_id is not None, (
            "a failed greeting send is not an error to the caller"
        )
        outbound = [
            m for m in await _messages(db_session, conv_id) if m.direction == "out"
        ]
        assert len(outbound) == 1, "the greeting persists despite the send failure"
        assert outbound[0].delivery == "failed", (
            "a failed send is a badged saved message"
        )


async def _count_messages(session: AsyncSession, conversation_id: int) -> int:
    """Return the number of stored messages for a conversation."""
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(SupportMessage)
                .where(SupportMessage.conversation_id == conversation_id)
            )
        ).scalar_one()
    )
