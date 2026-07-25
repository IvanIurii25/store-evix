"""Integration tests for the public Telegram webhook endpoint.

Drives ``POST /api/v1/telegram/webhook`` over HTTP (through the service into the
DB): secret-token auth (403 on missing/wrong, fail-closed when unset), the
always-200 ack for malformed / non-text updates, first-inbound conversation +
message creation with the inbox snapshot, dedup of a re-delivered update, and
snapshot refresh + append on an existing chat.
"""

from unittest.mock import Mock

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.api.routers.telegram as telegram_router
from app.core.config import settings
from app.models.support import SupportConversation, SupportMessage

pytestmark = pytest.mark.asyncio

_WEBHOOK_URL: str = "/api/v1/telegram/webhook"
_SECRET_HEADER: str = "X-Telegram-Bot-Api-Secret-Token"
_TEST_SECRET: str = "test-secret"
# Telegram ids used by the update fixtures.
_CHAT_ID: int = 555
_MESSAGE_ID: int = 10
# Staff group chat id used by the enqueue tests.
_STAFF_CHAT_ID: str = "-100999"
# Snippet cap the webhook applies to the enqueued text.
_SNIPPET_LEN: int = 200


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


def _start_update(*, message_id: int = _MESSAGE_ID) -> dict:
    """Return a valid ``/start`` deep-link update (bare payload → generic context)."""
    update = _update(message_id=message_id)
    update["message"]["text"] = "/start"
    return update


async def _count_conversations(session: AsyncSession) -> int:
    """Return the total number of persisted conversations."""
    return int(
        (
            await session.execute(select(func.count()).select_from(SupportConversation))
        ).scalar_one()
    )


async def _count_messages(session: AsyncSession) -> int:
    """Return the total number of persisted messages."""
    return int(
        (
            await session.execute(select(func.count()).select_from(SupportMessage))
        ).scalar_one()
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


def _nameless_update(*, message_id: int = _MESSAGE_ID) -> dict:
    """A valid text update whose sender has no first_name / username."""
    return {
        "update_id": 1,
        "message": {
            "message_id": message_id,
            "chat": {"id": _CHAT_ID},
            "from": {"id": _CHAT_ID, "language_code": "ro"},
            "text": "Salut",
        },
    }


class TelegramWebhookNotifyStaffTest:
    """Webhook enqueues ``notify_staff.delay`` once per fresh unread burst."""

    async def test_fresh_inbound_enqueues_notify_once(
        self,
        guest_client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: valid secret, a configured staff chat, and a spied task.
        monkeypatch.setattr(settings, "telegram_webhook_secret", _TEST_SECRET)
        monkeypatch.setattr(settings, "telegram_staff_chat_id", _STAFF_CHAT_ID)
        notify = Mock()
        monkeypatch.setattr(telegram_router, "notify_staff", notify)

        # Act: one valid inbound to a fresh chat.
        resp = await guest_client.post(
            _WEBHOOK_URL,
            json=_update(),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )

        # Assert: acked, and the ping was enqueued once with (id, name, snippet).
        assert resp.status_code == 200, resp.text
        conv = (
            await db_session.execute(
                select(SupportConversation).where(
                    SupportConversation.tg_chat_id == _CHAT_ID
                )
            )
        ).scalar_one()
        notify.delay.assert_called_once_with(conv.id, "Ana", "Salut"[:_SNIPPET_LEN])

    async def test_start_deep_link_enqueues_friendly_snippet(
        self,
        guest_client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: valid secret, a configured staff chat, and a spied task.
        monkeypatch.setattr(settings, "telegram_webhook_secret", _TEST_SECRET)
        monkeypatch.setattr(settings, "telegram_staff_chat_id", _STAFF_CHAT_ID)
        notify = Mock()
        monkeypatch.setattr(telegram_router, "notify_staff", notify)

        # Act: a "/start" deep-link opens a fresh chat (generic 'site' context).
        resp = await guest_client.post(
            _WEBHOOK_URL,
            json=_start_update(),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )

        # Assert: the ping snippet is the friendly context, NOT the raw command.
        assert resp.status_code == 200, resp.text
        _id, _name, snippet = notify.delay.call_args.args
        assert snippet == "🔗 Обращение с сайта", (
            "ping shows the friendly /start context"
        )
        assert not snippet.startswith("/start"), "ping must not show the raw command"

    async def test_name_falls_back_when_sender_anonymous(
        self,
        guest_client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: a sender with neither first_name nor username.
        monkeypatch.setattr(settings, "telegram_webhook_secret", _TEST_SECRET)
        monkeypatch.setattr(settings, "telegram_staff_chat_id", _STAFF_CHAT_ID)
        notify = Mock()
        monkeypatch.setattr(telegram_router, "notify_staff", notify)

        # Act: post the nameless inbound.
        resp = await guest_client.post(
            _WEBHOOK_URL,
            json=_nameless_update(),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )

        # Assert: the ping name falls back to the generic label.
        assert resp.status_code == 200, resp.text
        _, name, _snippet = notify.delay.call_args.args
        assert name == "клиент", "name must fall back when no name/username present"

    async def test_second_inbound_same_chat_not_enqueued_again(
        self,
        guest_client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: valid secret + staff chat + spied task.
        monkeypatch.setattr(settings, "telegram_webhook_secret", _TEST_SECRET)
        monkeypatch.setattr(settings, "telegram_staff_chat_id", _STAFF_CHAT_ID)
        notify = Mock()
        monkeypatch.setattr(telegram_router, "notify_staff", notify)
        headers = {_SECRET_HEADER: _TEST_SECRET}

        # Act: two inbounds on the same chat with no read in between → still unread.
        await guest_client.post(
            _WEBHOOK_URL, json=_update(message_id=10), headers=headers
        )
        await guest_client.post(
            _WEBHOOK_URL, json=_update(message_id=11), headers=headers
        )

        # Assert: debounced to a single ping for the burst.
        assert notify.delay.call_count == 1, "one ping per unread burst (debounced)"

    async def test_no_enqueue_when_staff_chat_unset(
        self,
        guest_client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: valid secret, but no staff chat configured (default empty).
        monkeypatch.setattr(settings, "telegram_webhook_secret", _TEST_SECRET)
        monkeypatch.setattr(settings, "telegram_staff_chat_id", "")
        notify = Mock()
        monkeypatch.setattr(telegram_router, "notify_staff", notify)

        # Act: a fresh inbound that would otherwise ping.
        resp = await guest_client.post(
            _WEBHOOK_URL,
            json=_update(),
            headers={_SECRET_HEADER: _TEST_SECRET},
        )

        # Assert: acked, but nothing enqueued (no staff chat → no ping).
        assert resp.status_code == 200, resp.text
        notify.delay.assert_not_called()
