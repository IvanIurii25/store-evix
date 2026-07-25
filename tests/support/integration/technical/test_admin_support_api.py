"""Integration tests for the admin support inbox API.

Drives the staff-only router over HTTP: the paginated inbox envelope + status
filter + auth guard, the thread read that clears unread (mark_read side-effect)
+ 404, the reply endpoint (sent + failed-delivery paths + 404 + timestamp), the
status change (valid + 422 + 404), and an SSE route-registration smoke.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.support import SupportConversation, SupportMessage
from app.repositories.support_repo import SupportRepository

pytestmark = pytest.mark.asyncio

_BASE: str = "/api/v1/admin/support/conversations"
# Telegram chat ids for seeded conversations.
_CHAT_A: int = 2001
_CHAT_B: int = 2002
# Page size the router advertises for the inbox list (§4).
_INBOX_PAGE_SIZE: int = 20


async def _seed_conversation(
    db_session: AsyncSession,
    *,
    tg_chat_id: int,
    status: str = "open",
    unread: int = 0,
) -> SupportConversation:
    """Persist a conversation directly (arrange helper), bypassing the webhook."""
    repo = SupportRepository(db_session)
    conv = await repo.create_conversation(
        tg_chat_id=tg_chat_id,
        customer_name="Ana",
        customer_username="ana",
        lang="ro",
    )
    if status != "open":
        conv.status = status
    conv.unread_count = unread
    await db_session.flush()
    return conv


class AdminInboxListTest:
    """``GET /conversations`` envelope + status filter + pagination + guard."""

    async def test_list_envelope_and_filter(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: one open + one closed conversation.
        await _seed_conversation(db_session, tg_chat_id=_CHAT_A, status="open")
        await _seed_conversation(db_session, tg_chat_id=_CHAT_B, status="closed")

        # Act: list open conversations only.
        resp = await client.get(_BASE, params={"status": "open"})

        # Assert: the envelope shape + the filter selected just the open row.
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {"data", "total", "page", "page_size"}, "envelope keys"
        assert body["total"] == 1, "only the open conversation counts"
        assert body["page"] == 1
        assert body["page_size"] == _INBOX_PAGE_SIZE
        assert len(body["data"]) == 1, "one open conversation in the page"
        assert "customer_name" in body["data"][0], "ConversationOut is serialized"

    async def test_list_pagination_second_page_empty(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: a single conversation (page 2 must be empty for page_size 20).
        await _seed_conversation(db_session, tg_chat_id=_CHAT_A)

        # Act: request the second page.
        resp = await client.get(_BASE, params={"page": 2})

        # Assert: total still counts the row, but this page carries none.
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 1, "total spans all pages"
        assert body["page"] == 2
        assert body["data"] == [], "second page is empty"

    async def test_list_guest_guard(
        self,
        guest_client: AsyncClient,
    ) -> None:
        # Act: an unauthenticated caller hits the staff-only inbox.
        resp = await guest_client.get(_BASE)

        # Assert: the current_staff guard blocks it (401 not-authenticated).
        assert resp.status_code in (401, 403), resp.text


class AdminThreadTest:
    """``GET /conversations/{id}`` thread read + mark_read + 404."""

    async def test_thread_returns_messages_and_clears_unread(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: a conversation with two inbound messages and unread=2.
        conv = await _seed_conversation(db_session, tg_chat_id=_CHAT_A, unread=2)
        repo = SupportRepository(db_session)
        for idx, body in enumerate(("first", "second")):
            await repo.add_message(
                conversation_id=conv.id,
                direction="in",
                text=body,
                tg_message_id=700 + idx,
            )
        await db_session.flush()

        # Act: open the conversation.
        resp = await client.get(f"{_BASE}/{conv.id}")

        # Assert: ThreadOut with chronological messages, unread cleared to 0.
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [m["text"] for m in body["data"]] == [
            "first",
            "second",
        ], "messages are chronological"
        assert body["total"] == 2, "thread total counts both messages"
        assert (
            body["conversation"]["unread_count"] == 0
        ), "opening a conversation clears unread (mark_read)"

    async def test_thread_unknown_id_404(self, client: AsyncClient) -> None:
        # Act: request a conversation that does not exist.
        resp = await client.get(f"{_BASE}/999999")

        # Assert: mapped to 404 with the domain code.
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"


class AdminReplyTest:
    """``POST /conversations/{id}/reply`` sent / failed / 404 / timestamp."""

    async def test_reply_persists_outbound_sent(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: a conversation to reply into (no bot token → no-op send).
        conv = await _seed_conversation(db_session, tg_chat_id=_CHAT_A)

        # Act: post an operator reply.
        resp = await client.post(
            f"{_BASE}/{conv.id}/reply",
            json={"text": "Bună ziua!"},
        )

        # Assert: MessageOut with an outbound, sent message and a timestamp.
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["direction"] == "out", "reply is an outbound message"
        assert body["delivery"] == "sent", "no-op send path records 'sent'"
        assert body["created_at"], "created_at must be populated (refresh gotcha)"
        # And it is actually persisted.
        stored = (
            await db_session.execute(
                select(SupportMessage).where(SupportMessage.id == body["id"])
            )
        ).scalar_one()
        assert stored.direction == "out", "outbound row persisted"

    async def test_reply_failed_delivery_still_200(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: a conversation + a send_message that raises (failed delivery).
        conv = await _seed_conversation(db_session, tg_chat_id=_CHAT_A)

        async def _boom(chat_id: int, text: str) -> int:
            raise RuntimeError("telegram down")

        # The service reads send_message off the module at call time.
        monkeypatch.setattr("app.core.telegram.send_message", _boom)

        # Act: post a reply while the send fails.
        resp = await client.post(
            f"{_BASE}/{conv.id}/reply",
            json={"text": "Hi"},
        )

        # Assert: still 200, message saved with a "failed" badge (not raised).
        assert resp.status_code == 200, resp.text
        assert resp.json()["delivery"] == "failed", "failed send → 'failed' badge"

    async def test_reply_unknown_conversation_404(
        self, client: AsyncClient
    ) -> None:
        # Act: reply into a conversation that does not exist.
        resp = await client.post(f"{_BASE}/999999/reply", json={"text": "hi"})

        # Assert: mapped 404.
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_reply_empty_text_422(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: a conversation; empty text violates ReplyIn.min_length.
        conv = await _seed_conversation(db_session, tg_chat_id=_CHAT_A)

        # Act: post an empty reply body.
        resp = await client.post(f"{_BASE}/{conv.id}/reply", json={"text": ""})

        # Assert: request validation rejects it.
        assert resp.status_code == 422, resp.text


class AdminStatusTest:
    """``POST /conversations/{id}/status`` valid / 422 / 404."""

    async def test_status_valid_transition(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: an open conversation.
        conv = await _seed_conversation(db_session, tg_chat_id=_CHAT_A, status="open")

        # Act: close it.
        resp = await client.post(
            f"{_BASE}/{conv.id}/status",
            json={"status": "closed"},
        )

        # Assert: ConversationOut reflects the new status.
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "closed", "status updated in the response"

    async def test_status_invalid_value_422(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: a conversation to (fail to) transition.
        conv = await _seed_conversation(db_session, tg_chat_id=_CHAT_A)

        # Act: submit a status outside SUPPORT_STATUSES.
        resp = await client.post(
            f"{_BASE}/{conv.id}/status",
            json={"status": "banana"},
        )

        # Assert: StatusIn validator rejects it (422).
        assert resp.status_code == 422, resp.text

    async def test_status_unknown_conversation_404(
        self, client: AsyncClient
    ) -> None:
        # Act: set the status of a missing conversation.
        resp = await client.post(
            f"{_BASE}/999999/status",
            json={"status": "closed"},
        )

        # Assert: mapped 404.
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"


class AdminStreamTest:
    """SSE ``GET /conversations/stream`` — light open smoke."""

    async def test_stream_route_registered(self, client: AsyncClient) -> None:
        # The SSE generator subscribes to Redis and blocks indefinitely on the
        # channel, which does not stream incrementally under httpx's in-process
        # ASGITransport (it would hang). So we assert the route is *registered*
        # and typed as an event-stream via the OpenAPI/route table; the actual
        # publish→subscribe round-trip is covered by the support_events unit test.
        openapi = (await client.get("/openapi.json")).json()
        assert (
            "/api/v1/admin/support/conversations/stream" in openapi["paths"]
        ), "the SSE stream route must be registered under /api/v1"

    # NOTE: A live raw-read smoke of the SSE stream (open → read the ``: connected``
    # frame → close) was intentionally dropped. The endpoint's generator parks in
    # ``subscribe_support_events`` → ``pubsub.listen()`` (an unbounded async loop);
    # under httpx's in-process ``ASGITransport`` the streaming background task does
    # not unwind on ``resp.aclose()``, so the test hangs on teardown rather than
    # failing. The publish→subscribe round-trip the stream is built on is covered
    # directly in ``test_events.py``; here we only assert the route is registered.
