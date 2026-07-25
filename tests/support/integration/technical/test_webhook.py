"""Integration tests for the public Telegram webhook endpoint.

Drives ``POST /api/v1/telegram/webhook`` over HTTP (through the service into the
DB): secret-token auth (403 on missing/wrong, fail-closed when unset), the
always-200 ack for malformed / non-text updates, first-inbound conversation +
message creation with the inbox snapshot, dedup of a re-delivered update, and
snapshot refresh + append on an existing chat.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.support import SupportConversation, SupportMessage

pytestmark = pytest.mark.asyncio

_WEBHOOK_URL: str = "/api/v1/telegram/webhook"
_SECRET_HEADER: str = "X-Telegram-Bot-Api-Secret-Token"
_TEST_SECRET: str = "test-secret"
# Telegram ids used by the update fixtures.
_CHAT_ID: int = 555
_MESSAGE_ID: int = 10


def _update(*, message_id: int = _MESSAGE_ID, username: str = "ana") -> dict:
    """Return a valid private text-message Telegram update dict."""
    return {
        "update_id": 1,
        "message": {
            "message_id": message_id,
            "chat": {"id": _CHAT_ID},
            "from": {
                "id": _CHAT_ID,
                "first_name": "Ana",
                "username": username,
                "language_code": "ro",
            },
            "text": "Salut",
        },
    }


async def _count_conversations(session: AsyncSession) -> int:
    """Return the total number of persisted conversations."""
    return int(
        (await session.execute(select(func.count()).select_from(SupportConversation)))
        .scalar_one()
    )


async def _count_messages(session: AsyncSession) -> int:
    """Return the total number of persisted messages."""
    return int(
        (await session.execute(select(func.count()).select_from(SupportMessage)))
        .scalar_one()
    )


class TelegramWebhookAuthTest:
    """Secret-token guard: 403 on missing / wrong / fail-closed."""

    async def test_missing_secret_header_403(
        self,
        guest_client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: a configured secret, but the caller sends no header.
        monkeypatch.setattr(settings, "telegram_webhook_secret", _TEST_SECRET)

        # Act: post a valid update without the secret header.
        resp = await guest_client.post(_WEBHOOK_URL, json=_update())

        # Assert: rejected before any parsing.
        assert resp.status_code == 403, resp.text

    async def test_wrong_secret_header_403(
        self,
        guest_client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: a configured secret and a mismatching header.
        monkeypatch.setattr(settings, "telegram_webhook_secret", _TEST_SECRET)

        # Act: post with the wrong secret.
        resp = await guest_client.post(
            _WEBHOOK_URL,
            json=_update(),
            headers={_SECRET_HEADER: "wrong"},
        )

        # Assert: rejected.
        assert resp.status_code == 403, resp.text

    async def test_unset_secret_fails_closed_403(
        self,
        guest_client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: no secret configured — even a header must not authenticate.
        monkeypatch.setattr(settings, "telegram_webhook_secret", "")

        # Act: post with a header while the config secret is empty.
        resp = await guest_client.post(
            _WEBHOOK_URL,
            json=_update(),
            headers={_SECRET_HEADER: "anything"},
        )

        # Assert: fails closed.
        assert resp.status_code == 403, resp.text


class TelegramWebhookAckTest:
    """Always-200 ack for updates this MVP does not persist."""

    async def test_malformed_json_acked_no_rows(
        self,
        guest_client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: a valid secret; body is invalid JSON.
        monkeypatch.setattr(settings, "telegram_webhook_secret", _TEST_SECRET)

        # Act: post a non-JSON body (Telegram retries un-acked calls, so ack).
        resp = await guest_client.post(
            _WEBHOOK_URL,
            content=b"not-json",
            headers={
                _SECRET_HEADER: _TEST_SECRET,
                "Content-Type": "application/json",
            },
        )

        # Assert: 200 ack, and nothing was written.
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True}
        assert await _count_conversations(db_session) == 0, "malformed → no rows"

    async def test_non_text_update_acked_no_rows(
        self,
        guest_client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: a valid secret; a non-text update (a photo message).
        monkeypatch.setattr(settings, "telegram_webhook_secret", _TEST_SECRET)
        photo = {
            "update_id": 1,
            "message": {"message_id": 1, "chat": {"id": _CHAT_ID}, "photo": []},
        }

        # Act: post it.
        resp = await guest_client.post(
            _WEBHOOK_URL,
            json=photo,
            headers={_SECRET_HEADER: _TEST_SECRET},
        )

        # Assert: 200 ack, no persistence (parse_inbound returned None).
        assert resp.status_code == 200, resp.text
        assert await _count_conversations(db_session) == 0, "non-text → no rows"


class TelegramWebhookInboundTest:
    """Valid inbound: create, dedup, snapshot refresh + append."""

    async def test_valid_inbound_creates_conversation_and_message(
        self,
        guest_client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: a valid secret and a fresh chat.
        monkeypatch.setattr(settings, "telegram_webhook_secret", _TEST_SECRET)

        # Act: post one valid inbound update.
        resp = await guest_client.post(
            _WEBHOOK_URL,
            json=_update(),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )

        # Assert: 200 ack + exactly one conversation with the snapshot fields.
        assert resp.status_code == 200, resp.text
        conv = (
            await db_session.execute(
                select(SupportConversation).where(
                    SupportConversation.tg_chat_id == _CHAT_ID
                )
            )
        ).scalar_one()
        assert conv.customer_name == "Ana", "snapshot name from the update"
        assert conv.customer_username == "ana", "snapshot username from the update"
        assert conv.lang == "ro", "snapshot lang from the update"
        assert conv.status == "open", "new conversation is open"
        assert conv.unread_count == 1, "the one inbound message is unread"
        assert await _count_messages(db_session) == 1, "one inbound message stored"

    async def test_duplicate_update_deduped(
        self,
        guest_client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: a valid secret; the same update delivered twice.
        monkeypatch.setattr(settings, "telegram_webhook_secret", _TEST_SECRET)
        headers = {_SECRET_HEADER: _TEST_SECRET}

        # Act: post the identical update (same message_id) twice.
        first = await guest_client.post(_WEBHOOK_URL, json=_update(), headers=headers)
        second = await guest_client.post(_WEBHOOK_URL, json=_update(), headers=headers)

        # Assert: both acked, but only one message persisted (deduped).
        assert first.status_code == 200 and second.status_code == 200
        assert await _count_messages(db_session) == 1, "re-delivery must dedup"
        assert await _count_conversations(db_session) == 1, "still one conversation"

    async def test_existing_chat_refreshes_snapshot_and_appends(
        self,
        guest_client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: a first message establishes the conversation.
        monkeypatch.setattr(settings, "telegram_webhook_secret", _TEST_SECRET)
        headers = {_SECRET_HEADER: _TEST_SECRET}
        await guest_client.post(
            _WEBHOOK_URL,
            json=_update(message_id=10, username="ana"),
            headers=headers,
        )

        # Act: a second message on the same chat with a NEW username + id.
        resp = await guest_client.post(
            _WEBHOOK_URL,
            json=_update(message_id=11, username="ana_new"),
            headers=headers,
        )

        # Assert: still one conversation, snapshot refreshed, two messages, unread 2.
        assert resp.status_code == 200, resp.text
        assert await _count_conversations(db_session) == 1, "no new conversation"
        assert await _count_messages(db_session) == 2, "second message appended"
        conv = (
            await db_session.execute(
                select(SupportConversation).where(
                    SupportConversation.tg_chat_id == _CHAT_ID
                )
            )
        ).scalar_one()
        assert conv.customer_username == "ana_new", "snapshot must refresh on append"
        assert conv.unread_count == 2, "two unread inbound messages"
