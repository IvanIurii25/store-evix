"""API tests for the admin restock-demand overview (§9).

Covers the empty case, the aggregate (per-product waiter count, 7-day momentum,
active-only filtering, ordering by waiters desc, and the carried catalog
context), and the staff auth guard.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.restock import (
    STATUS_ACTIVE,
    STATUS_NOTIFIED,
    RestockSubscription,
)
from app.models.user import AppUser
from tests.restock.conftest import make_product

pytestmark = pytest.mark.asyncio

_DEMAND_URL = "/api/v1/admin/restock/demand"


async def _make_user(db_session: AsyncSession, *, email: str) -> AppUser:
    """Persist and return a minimal customer for a subscription."""
    user = AppUser(
        email=email,
        password_hash="x",
        is_active=True,
        is_staff=False,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def _add_subscription(
    db_session: AsyncSession,
    *,
    product_id: int,
    user_id: int,
    status: str = STATUS_ACTIVE,
    created_at: datetime | None = None,
) -> None:
    """Persist one restock subscription, optionally back-dating ``created_at``."""
    subscription = RestockSubscription(
        product_id=product_id,
        user_id=user_id,
        lang="ro",
        status=status,
    )
    db_session.add(subscription)
    await db_session.flush()
    if created_at is not None:
        subscription.created_at = created_at
        await db_session.flush()


async def test_demand_empty(staff_client: AsyncClient) -> None:
    """With no active subscriptions the overview is an empty list."""
    resp = await staff_client.get(_DEMAND_URL)
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


async def test_demand_aggregate(
    staff_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Aggregate: active-only counting, 7-day momentum, ordering, and context."""
    # ``created_at`` is TIMESTAMP WITHOUT TIME ZONE (stored UTC) — use naive UTC.
    now = datetime.now(UTC).replace(tzinfo=None)
    old = now - timedelta(days=30)

    # Product A: 3 active waiters (2 recent, 1 old) -> waiters=3, waiters_7d=2.
    prod_a = await make_product(db_session, code="dem-a", qty=0, slug="alpha")
    # Product B: 2 active waiters (both old) -> waiters=2, waiters_7d=0.
    prod_b = await make_product(db_session, code="dem-b", qty=0, slug="beta")
    # Product C: only a NOTIFIED sub -> must NOT appear.
    prod_c = await make_product(db_session, code="dem-c", qty=0, slug="gamma")

    users = [await _make_user(db_session, email=f"w{i}@example.com") for i in range(5)]

    await _add_subscription(
        db_session, product_id=prod_a.id, user_id=users[0].id, created_at=now
    )
    await _add_subscription(
        db_session, product_id=prod_a.id, user_id=users[1].id, created_at=now
    )
    await _add_subscription(
        db_session, product_id=prod_a.id, user_id=users[2].id, created_at=old
    )
    await _add_subscription(
        db_session, product_id=prod_b.id, user_id=users[3].id, created_at=old
    )
    await _add_subscription(
        db_session, product_id=prod_b.id, user_id=users[4].id, created_at=old
    )
    await _add_subscription(
        db_session,
        product_id=prod_c.id,
        user_id=users[0].id,
        status=STATUS_NOTIFIED,
    )

    resp = await staff_client.get(f"{_DEMAND_URL}?lang=ro")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Only A and B appear (C is notified-only), ordered by waiters desc.
    assert [item["product_id"] for item in body] == [prod_a.id, prod_b.id]

    item_a, item_b = body
    assert item_a["waiters"] == 3
    assert item_a["waiters_7d"] == 2
    assert item_a["name"] == "Phone ro"
    assert item_a["slug"] == "alpha-ro"
    assert item_a["qty"] == 0
    assert item_a["in_stock"] is False
    assert item_a["price"] == "100.00"
    assert item_a["is_active"] is True
    assert item_a["category"] is None  # make_product adds no category translation

    assert item_b["waiters"] == 2
    assert item_b["waiters_7d"] == 0
    assert item_b["slug"] == "beta-ro"


async def test_demand_guest_forbidden(guest_client: AsyncClient) -> None:
    """A non-staff caller cannot read the demand overview (staff guard)."""
    resp = await guest_client.get(_DEMAND_URL)
    assert resp.status_code in (401, 403), resp.text
