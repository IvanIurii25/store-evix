"""API + service tests for restock subscriptions (Phase 1, §5).

Covers subscribe (create / reactivate / idempotent / reject-in-stock-400 /
404-missing / 401-guest), unsubscribe, the button-state read, the account list,
and the admin waiter-count endpoint.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.restock import (
    STATUS_ACTIVE,
    STATUS_NOTIFIED,
    RestockSubscription,
)
from app.repositories.restock_repo import RestockRepository
from tests.restock.conftest import TEST_USER_ID, make_product

pytestmark = pytest.mark.asyncio


async def test_subscribe_creates_active(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Subscribing to an OOS product returns ``{subscribed:true}`` + an active row."""
    product = await make_product(db_session, code="p-oos", qty=0)

    resp = await client.post(
        "/api/v1/restock/subscriptions",
        json={"product_id": product.id, "lang": "ru"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"subscribed": True}

    row = await RestockRepository(db_session).get(product.id, TEST_USER_ID)
    assert row is not None
    assert row.status == STATUS_ACTIVE
    assert row.lang == "ru"


async def test_subscribe_idempotent(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A second subscribe is a no-op — still exactly one active row."""
    product = await make_product(db_session, code="p-oos2", qty=0)
    for _ in range(2):
        resp = await client.post(
            "/api/v1/restock/subscriptions",
            json={"product_id": product.id, "lang": "ro"},
        )
        assert resp.status_code == 200, resp.text

    rows = await RestockRepository(db_session).list_active_for_product(product.id)
    assert len(rows) == 1


async def test_subscribe_reactivates_notified(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Re-subscribing a spent (notified) row reactivates it + clears notified_at."""
    product = await make_product(db_session, code="p-oos3", qty=0)
    # Seed a spent subscription directly.
    db_session.add(
        RestockSubscription(
            product_id=product.id,
            user_id=TEST_USER_ID,
            lang="ru",
            status=STATUS_NOTIFIED,
        )
    )
    await db_session.flush()

    resp = await client.post(
        "/api/v1/restock/subscriptions",
        json={"product_id": product.id, "lang": "ro"},
    )
    assert resp.status_code == 200, resp.text

    row = await RestockRepository(db_session).get(product.id, TEST_USER_ID)
    assert row.status == STATUS_ACTIVE
    assert row.notified_at is None
    assert row.lang == "ro"


async def test_subscribe_in_stock_rejected_400(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Subscribing to an in-stock product is rejected with 400 (§8.1)."""
    product = await make_product(db_session, code="p-in", qty=5)

    resp = await client.post(
        "/api/v1/restock/subscriptions",
        json={"product_id": product.id, "lang": "ru"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "in_stock"


async def test_subscribe_variable_rejected_400(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A variable product opts out of restock in v1 → 400 ``unsupported`` (P2f)."""
    product = await make_product(db_session, code="p-var", qty=0)
    product.has_variants = True
    await db_session.flush()

    resp = await client.post(
        "/api/v1/restock/subscriptions",
        json={"product_id": product.id, "lang": "ru"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "unsupported"


async def test_subscribe_missing_product_404(client: AsyncClient) -> None:
    """Subscribing to a nonexistent product is a 404."""
    resp = await client.post(
        "/api/v1/restock/subscriptions",
        json={"product_id": 999999, "lang": "ru"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "not_found"


async def test_subscribe_guest_401(
    guest_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A guest subscribe attempt is 401 (front reads this as "login needed")."""
    product = await make_product(db_session, code="p-guest", qty=0)
    resp = await guest_client.post(
        "/api/v1/restock/subscriptions",
        json={"product_id": product.id, "lang": "ru"},
    )
    assert resp.status_code == 401, resp.text


async def test_unsubscribe(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """DELETE removes the subscription and returns 204."""
    product = await make_product(db_session, code="p-unsub", qty=0)
    await client.post(
        "/api/v1/restock/subscriptions",
        json={"product_id": product.id, "lang": "ru"},
    )

    resp = await client.delete(f"/api/v1/restock/subscriptions/{product.id}")
    assert resp.status_code == 204, resp.text
    assert await RestockRepository(db_session).get(product.id, TEST_USER_ID) is None


async def test_is_subscribed_state(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The button-state read flips false → true after subscribing."""
    product = await make_product(db_session, code="p-state", qty=0)

    before = await client.get(f"/api/v1/restock/subscriptions/{product.id}")
    assert before.json() == {"subscribed": False}

    await client.post(
        "/api/v1/restock/subscriptions",
        json={"product_id": product.id, "lang": "ru"},
    )
    after = await client.get(f"/api/v1/restock/subscriptions/{product.id}")
    assert after.json() == {"subscribed": True}


async def test_list_my_subscriptions(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The account list returns the user's active items resolved in the lang."""
    product = await make_product(db_session, code="p-list", qty=0, slug="watch")
    await client.post(
        "/api/v1/restock/subscriptions",
        json={"product_id": product.id, "lang": "ru"},
    )

    resp = await client.get("/api/v1/restock/subscriptions?lang=ro")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["product_id"] == product.id
    assert body[0]["slug"] == "watch-ro"
    assert body[0]["name"] == "Phone ro"


async def test_admin_waiter_count(
    client: AsyncClient,
    staff_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The admin waiter-count endpoint reports the active-subscriber count (§9)."""
    product = await make_product(db_session, code="p-count", qty=0)
    await client.post(
        "/api/v1/restock/subscriptions",
        json={"product_id": product.id, "lang": "ru"},
    )

    resp = await staff_client.get(
        f"/api/v1/admin/products/{product.id}/restock-waiters"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"count": 1}
