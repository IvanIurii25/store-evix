"""API tests for the public consent endpoint (LP195/2024, Art.7).

Drives ``POST /api/v1/consent`` through the full app and asserts the append-only
record: category snapshot, action/source, and that each decision is a new row.
"""

import pytest
from sqlalchemy import func, select

from app.models.consent import CONSENT_POLICY_VERSION, ConsentRecord

pytestmark = pytest.mark.asyncio


async def _count(db_session) -> int:
    return await db_session.scalar(select(func.count()).select_from(ConsentRecord))


class TestConsentRecord:
    async def test_accept_all_records_analytics_granted(self, async_client, db_session):
        """accept_all persists a record with analytics granted + echoes version."""
        resp = await async_client.post(
            "/api/v1/consent",
            json={
                "analytics": True,
                "action": "accept_all",
                "source": "banner",
                "lang": "ro",
                "anonymous_id": "aid-123",
            },
        )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "policy_version": CONSENT_POLICY_VERSION}
        row = (await db_session.execute(select(ConsentRecord))).scalars().one()
        assert row.categories == {"necessary": True, "analytics": True}
        assert row.action == "accept_all"
        assert row.anonymous_id == "aid-123"
        assert row.policy_version == CONSENT_POLICY_VERSION

    async def test_reject_records_analytics_denied(self, async_client, db_session):
        """reject_all persists a record with analytics denied."""
        resp = await async_client.post(
            "/api/v1/consent",
            json={"analytics": False, "action": "reject_all", "lang": "ru"},
        )

        assert resp.status_code == 200, resp.text
        row = (await db_session.execute(select(ConsentRecord))).scalars().one()
        assert row.categories["analytics"] is False
        assert row.source == "banner"  # default

    async def test_each_decision_is_a_new_row_append_only(
        self, async_client, db_session
    ):
        """Changing the decision writes a second row (never updates)."""
        await async_client.post(
            "/api/v1/consent",
            json={"analytics": True, "action": "accept_all", "lang": "ro"},
        )
        await async_client.post(
            "/api/v1/consent",
            json={
                "analytics": False,
                "action": "withdraw",
                "source": "settings",
                "lang": "ro",
            },
        )

        assert await _count(db_session) == 2

    async def test_invalid_action_rejected_422(self, async_client, db_session):
        """An action outside the allowed set is a 422 and stores nothing."""
        resp = await async_client.post(
            "/api/v1/consent",
            json={"analytics": True, "action": "bogus", "lang": "ro"},
        )

        assert resp.status_code == 422
        assert await _count(db_session) == 0
