"""Submit-review tests (Reviews & Ratings §7).

Covers create → pending, edit → pending (re-moderate), bad rating → 422, missing
product → 404, guest → 401, and the ``is_verified`` snapshot for both a
purchaser (true) and a non-purchaser (false).
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import STATUS_APPROVED, Review
from tests.reviews.conftest import (
    TEST_USER_ID,
    make_order_for,
    make_product,
)

pytestmark = pytest.mark.asyncio

_URL = "/api/v1/reviews"


async def test_submit_creates_pending(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A first submission creates the review with status ``pending``."""
    product = await make_product(db_session, code="sub-create")
    resp = await client.post(
        _URL,
        json={"product_id": product.id, "rating": 5, "body": "Great", "lang": "ru"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "pending"
    assert payload["rating"] == 5
    assert payload["product_id"] == product.id


async def test_submit_rejects_oversized_body(client: AsyncClient) -> None:
    """A review body over the max length is rejected by validation (422)."""
    resp = await client.post(
        _URL,
        json={"product_id": 1, "rating": 5, "body": "x" * 4001, "lang": "ru"},
    )
    assert resp.status_code == 422


async def test_submit_edit_returns_to_pending(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Editing an already-approved review sends it back to ``pending`` (§6.1)."""
    product = await make_product(db_session, code="sub-edit")
    first = await client.post(
        _URL, json={"product_id": product.id, "rating": 4, "lang": "ru"}
    )
    review_id = first.json()["id"]

    # Simulate the review having been approved, then edit it.
    review = await db_session.get(Review, review_id)
    review.status = STATUS_APPROVED
    await db_session.flush()

    second = await client.post(
        _URL,
        json={"product_id": product.id, "rating": 2, "body": "Changed", "lang": "ru"},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["id"] == review_id  # same row (idempotent on product+user)
    assert body["status"] == "pending"
    assert body["rating"] == 2


async def test_submit_bad_rating_rejected(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A rating outside 1..5 fails schema validation with 422."""
    product = await make_product(db_session, code="sub-bad")
    resp = await client.post(
        _URL, json={"product_id": product.id, "rating": 6, "lang": "ru"}
    )
    assert resp.status_code == 422


async def test_submit_missing_product_404(client: AsyncClient) -> None:
    """Reviewing a non-existent product returns 404."""
    resp = await client.post(
        _URL, json={"product_id": 999999, "rating": 3, "lang": "ru"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_submit_guest_401(
    guest_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A guest (no auth) is rejected with 401 by ``current_user``."""
    product = await make_product(db_session, code="sub-guest")
    resp = await guest_client.post(
        _URL, json={"product_id": product.id, "rating": 3, "lang": "ru"}
    )
    assert resp.status_code == 401


async def test_submit_verified_true_when_purchased(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A reviewer with a non-cancelled order line is snapshotted verified."""
    product = await make_product(db_session, code="sub-ver-t")
    await make_order_for(
        db_session,
        user_id=TEST_USER_ID,
        product_id=product.id,
        number="ORD-VER-1",
        status="confirmed",
    )
    resp = await client.post(
        _URL, json={"product_id": product.id, "rating": 5, "lang": "ru"}
    )
    assert resp.status_code == 200
    assert resp.json()["is_verified"] is True


async def test_submit_verified_false_without_purchase(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A reviewer with no (or only cancelled) orders is not verified."""
    product = await make_product(db_session, code="sub-ver-f")
    await make_order_for(
        db_session,
        user_id=TEST_USER_ID,
        product_id=product.id,
        number="ORD-VER-2",
        status="canceled",  # cancelled → does not count
    )
    resp = await client.post(
        _URL, json={"product_id": product.id, "rating": 5, "lang": "ru"}
    )
    assert resp.status_code == 200
    assert resp.json()["is_verified"] is False
