"""Integration tests for the admin support-metrics endpoint.

Drives ``GET /api/v1/admin/support/metrics`` (staff-only) over HTTP and asserts
the assembled :class:`SupportMetricsOut`: the conversation totals + current
status split, the ``unanswered`` waiting-on-operator count, the average
first-response time (real operator replies only — the bot auto-greeting, sent
with ``sender_staff_id = None``, must NOT count), the per-day new-conversation
series, and the trailing ``days`` window boundary (``now - days``). The seed
writes conversations + messages directly on ``db_session`` via
``SupportRepository`` and sets ``created_at``/``status`` explicitly where an
assertion depends on the timestamp or the current state.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.support import SupportConversation
from app.repositories.support_repo import SupportRepository

pytestmark = pytest.mark.asyncio

_METRICS: str = "/api/v1/admin/support/metrics"

# Telegram chat ids are unique per conversation; a monotonic base keeps every
# seeded conversation distinct without hand-numbering each call site.
_CHAT_BASE: int = 3000

# The first-response delta the qualifying conversation is seeded with: an
# inbound at T and a real operator outbound at T + 120s -> 120.0 seconds.
_FIRST_RESPONSE_SECONDS: float = 120.0

# A staff id that stands for a real operator reply (``sender_staff_id`` set, as
# opposed to the bot auto-greeting which leaves it NULL).
_OPERATOR_STAFF_ID: int = 7001


async def _seed_conversation(
    db_session: AsyncSession,
    *,
    chat_id: int,
    status: str = "open",
    created_at: datetime | None = None,
) -> SupportConversation:
    """Persist one conversation, optionally pinning its status/``created_at``.

    Args:
        db_session: The commit-safe test session.
        chat_id: Unique Telegram chat id for the conversation.
        status: Current conversation status to assert against.
        created_at: Explicit creation instant (controls the window boundary);
            left to the DB default (now) when ``None``.

    Returns:
        SupportConversation: The flushed conversation row.
    """
    repo = SupportRepository(db_session)
    conv = await repo.create_conversation(
        tg_chat_id=chat_id,
        customer_name="Ana",
        customer_username="ana",
        lang="ro",
    )
    if status != "open":
        conv.status = status
    if created_at is not None:
        conv.created_at = created_at
    await db_session.flush()
    return conv


async def _add_message(
    db_session: AsyncSession,
    *,
    conversation_id: int,
    direction: str,
    created_at: datetime,
    sender_staff_id: int | None = None,
) -> None:
    """Append one message and pin its ``created_at`` (controls response deltas).

    Args:
        db_session: The commit-safe test session.
        conversation_id: The owning conversation's primary key.
        direction: ``"in"`` (customer) or ``"out"`` (operator/bot).
        created_at: Explicit message instant (drives last-message + first-
            response ordering).
        sender_staff_id: Operator id for a real outbound reply; ``None`` marks an
            inbound message or the bot auto-greeting.
    """
    repo = SupportRepository(db_session)
    msg = await repo.add_message(
        conversation_id=conversation_id,
        direction=direction,
        text="hi",
        sender_staff_id=sender_staff_id,
    )
    msg.created_at = created_at
    await db_session.flush()


class SupportMetricsSummaryTest:
    """``GET /metrics`` — totals, status split, and response shape."""

    async def test_summary_counts_and_status_split(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: three current conversations across all three statuses, all
        # created in-period (default 30-day window).
        await _seed_conversation(db_session, chat_id=_CHAT_BASE + 1, status="open")
        await _seed_conversation(db_session, chat_id=_CHAT_BASE + 2, status="pending")
        await _seed_conversation(db_session, chat_id=_CHAT_BASE + 3, status="closed")

        # Act: fetch the metrics overview.
        resp = await client.get(_METRICS)

        # Assert: SupportMetricsOut shape + the total/new/status counts.
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {
            "total",
            "new_in_period",
            "open",
            "pending",
            "closed",
            "unanswered",
            "avg_first_response_seconds",
            "series",
            "days",
        }, "SupportMetricsOut keys"
        assert body["total"] == 3, "all three conversations count toward total"
        assert body["new_in_period"] == 3, "all three created within the 30d window"
        assert body["open"] == 1, "one open conversation"
        assert body["pending"] == 1, "one pending conversation"
        assert body["closed"] == 1, "one closed conversation"
        assert body["days"] == 30, "default window echoed"


class SupportMetricsUnansweredTest:
    """``unanswered`` — non-closed conversations whose last message is inbound."""

    async def test_unanswered_counts_only_waiting_conversations(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        now = datetime.now(UTC)

        # Arrange (counts): open, last message inbound -> waiting on operator.
        waiting = await _seed_conversation(
            db_session, chat_id=_CHAT_BASE + 10, status="open"
        )
        await _add_message(
            db_session,
            conversation_id=waiting.id,
            direction="in",
            created_at=now - timedelta(minutes=1),
        )

        # Arrange (does NOT count): open, but the operator replied last.
        answered = await _seed_conversation(
            db_session, chat_id=_CHAT_BASE + 11, status="open"
        )
        await _add_message(
            db_session,
            conversation_id=answered.id,
            direction="in",
            created_at=now - timedelta(minutes=2),
        )
        await _add_message(
            db_session,
            conversation_id=answered.id,
            direction="out",
            created_at=now - timedelta(minutes=1),
            sender_staff_id=_OPERATOR_STAFF_ID,
        )

        # Arrange (does NOT count): closed, even with a trailing inbound.
        closed = await _seed_conversation(
            db_session, chat_id=_CHAT_BASE + 12, status="closed"
        )
        await _add_message(
            db_session,
            conversation_id=closed.id,
            direction="in",
            created_at=now - timedelta(minutes=1),
        )

        # Arrange (does NOT count): a conversation with no messages at all.
        await _seed_conversation(db_session, chat_id=_CHAT_BASE + 13, status="open")

        # Act.
        resp = await client.get(_METRICS)

        # Assert: only the single waiting conversation is unanswered.
        assert resp.status_code == 200, resp.text
        assert resp.json()["unanswered"] == 1, (
            "only a non-closed conversation whose last message is inbound counts"
        )


class SupportMetricsFirstResponseTest:
    """``avg_first_response_seconds`` — real operator replies only."""

    async def test_avg_first_response_counts_only_real_operator_reply(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        now = datetime.now(UTC)

        # Arrange (qualifies): inbound at T, real operator outbound at T + 120s.
        qualifying = await _seed_conversation(
            db_session, chat_id=_CHAT_BASE + 20, created_at=now - timedelta(hours=1)
        )
        base = now - timedelta(minutes=30)
        await _add_message(
            db_session,
            conversation_id=qualifying.id,
            direction="in",
            created_at=base,
        )
        await _add_message(
            db_session,
            conversation_id=qualifying.id,
            direction="out",
            created_at=base + timedelta(seconds=_FIRST_RESPONSE_SECONDS),
            sender_staff_id=_OPERATOR_STAFF_ID,
        )

        # Arrange (does NOT qualify): the only outbound is the bot auto-greeting
        # (sender_staff_id is None) -> not a real reply.
        greeting_only = await _seed_conversation(
            db_session, chat_id=_CHAT_BASE + 21, created_at=now - timedelta(hours=1)
        )
        await _add_message(
            db_session,
            conversation_id=greeting_only.id,
            direction="in",
            created_at=base,
        )
        await _add_message(
            db_session,
            conversation_id=greeting_only.id,
            direction="out",
            created_at=base + timedelta(seconds=5),
            sender_staff_id=None,
        )

        # Arrange (does NOT qualify): an inbound with no reply at all.
        no_reply = await _seed_conversation(
            db_session, chat_id=_CHAT_BASE + 22, created_at=now - timedelta(hours=1)
        )
        await _add_message(
            db_session,
            conversation_id=no_reply.id,
            direction="in",
            created_at=base,
        )

        # Act.
        resp = await client.get(_METRICS)

        # Assert: the average reflects only the single qualifying conversation.
        assert resp.status_code == 200, resp.text
        avg = resp.json()["avg_first_response_seconds"]
        assert avg == pytest.approx(_FIRST_RESPONSE_SECONDS), (
            "only the real-operator reply (120s) contributes to the average"
        )

    async def test_avg_first_response_none_when_nothing_qualifies(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        now = datetime.now(UTC)

        # Arrange: an in-period conversation with an inbound but no real reply
        # (only the bot greeting) -> no qualifying conversation exists.
        conv = await _seed_conversation(
            db_session, chat_id=_CHAT_BASE + 30, created_at=now - timedelta(hours=1)
        )
        base = now - timedelta(minutes=10)
        await _add_message(
            db_session, conversation_id=conv.id, direction="in", created_at=base
        )
        await _add_message(
            db_session,
            conversation_id=conv.id,
            direction="out",
            created_at=base + timedelta(seconds=5),
            sender_staff_id=None,
        )

        # Act.
        resp = await client.get(_METRICS)

        # Assert: no qualifying conversation -> the average is null.
        assert resp.status_code == 200, resp.text
        assert resp.json()["avg_first_response_seconds"] is None, (
            "avg is None when no conversation received a real operator reply"
        )


class SupportMetricsSeriesWindowTest:
    """``series`` per-day shape + the trailing ``days`` window boundary."""

    async def test_series_buckets_new_conversations_per_day(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        now = datetime.now(UTC)
        # Two conversations on one day, one on another (both inside 30d).
        day_a = now - timedelta(days=3)
        day_b = now - timedelta(days=5)

        await _seed_conversation(db_session, chat_id=_CHAT_BASE + 40, created_at=day_a)
        await _seed_conversation(db_session, chat_id=_CHAT_BASE + 41, created_at=day_a)
        await _seed_conversation(db_session, chat_id=_CHAT_BASE + 42, created_at=day_b)

        # Act.
        resp = await client.get(_METRICS)

        # Assert: one point per distinct day, each carrying (day, count).
        assert resp.status_code == 200, resp.text
        series = resp.json()["series"]
        assert all(set(point) == {"day", "count"} for point in series), (
            "each series point is a (day, count) pair"
        )
        counts = {point["day"]: point["count"] for point in series}
        assert counts[day_a.date().isoformat()] == 2, "two conversations on day A"
        assert counts[day_b.date().isoformat()] == 1, "one conversation on day B"

    async def test_days_param_narrows_the_window(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        now = datetime.now(UTC)
        # In the 30d window but outside the 7d window (older than 7, within 30).
        older = now - timedelta(days=10)
        await _seed_conversation(db_session, chat_id=_CHAT_BASE + 50, created_at=older)

        # Act: default 30d includes it; a 7d window excludes it.
        resp_default = await client.get(_METRICS)
        resp_week = await client.get(_METRICS, params={"days": 7})

        # Assert: the older conversation counts for 30d, not for 7d.
        assert resp_default.status_code == 200, resp_default.text
        assert resp_week.status_code == 200, resp_week.text
        wide = resp_default.json()
        narrow = resp_week.json()
        assert wide["new_in_period"] == 1, "counted within the default 30d window"
        assert len(wide["series"]) == 1, "and present in the 30d series"
        assert narrow["new_in_period"] == 0, "outside the narrowed 7d window"
        assert narrow["series"] == [], "and absent from the 7d series"
        assert narrow["days"] == 7, "the requested window size is echoed back"


class SupportMetricsGuardTest:
    """``GET /metrics`` — staff-only guard."""

    async def test_metrics_guest_guard(self, guest_client: AsyncClient) -> None:
        # Act: an unauthenticated caller hits the staff-only metrics endpoint.
        resp = await guest_client.get(_METRICS)

        # Assert: the current_staff guard blocks it.
        assert resp.status_code in (401, 403), resp.text
