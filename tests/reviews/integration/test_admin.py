"""Admin moderation tests (Reviews & Ratings §7).

Approve → the review becomes visible publicly; reject → it stays hidden. Plus the
staff guard (401 guest / 403 non-staff), the pending-count badge and admin delete.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import Review
from tests.reviews.conftest import make_product, make_user

pytestmark = pytest.mark.asyncio

_ADMIN = "/api/v1/admin/reviews"


async def _seed_pending(
    db_session: AsyncSession,
    *,
    product_id: int,
    user_id: int,
    rating: int = 5,
) -> Review:
    """Persist a pending review awaiting moderation."""
    await make_user(db_session, user_id)
    review = Review(
        product_id=product_id,
        user_id=user_id,
        rating=rating,
        status="pending",
        is_verified=False,
        lang="ru",
    )
    db_session.add(review)
    await db_session.flush()
    return review


async def test_admin_approve_makes_public(
    staff_client: AsyncClient,
    guest_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Approving a pending review makes it visible in the public list."""
    product = await make_product(db_session, code="adm-appr", slug="ap")
    review = await _seed_pending(db_session, product_id=product.id, user_id=8400)

    # Hidden before approval.
    before = await guest_client.get(
        "/api/v1/catalog/products/ap-ru/reviews?lang=ru"
    )
    assert before.json()["data"] == []

    resp = await staff_client.patch(
        f"{_ADMIN}/{review.id}", json={"status": "approved"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["moderated_at"] is not None

    after = await guest_client.get(
        "/api/v1/catalog/products/ap-ru/reviews?lang=ru"
    )
    assert len(after.json()["data"]) == 1


async def test_admin_reject_keeps_hidden(
    staff_client: AsyncClient,
    guest_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Rejecting a review keeps it out of the public list."""
    product = await make_product(db_session, code="adm-rej", slug="rj")
    review = await _seed_pending(db_session, product_id=product.id, user_id=8401)

    resp = await staff_client.patch(
        f"{_ADMIN}/{review.id}", json={"status": "rejected"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    public = await guest_client.get(
        "/api/v1/catalog/products/rj-ru/reviews?lang=ru"
    )
    assert public.json()["data"] == []


async def test_admin_queue_filters_by_status(
    staff_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The moderation queue can be filtered to pending reviews only."""
    product = await make_product(db_session, code="adm-queue", slug="q")
    pending = await _seed_pending(db_session, product_id=product.id, user_id=8500)
    approved = await _seed_pending(db_session, product_id=product.id, user_id=8501)
    approved.status = "approved"
    await db_session.flush()

    resp = await staff_client.get(f"{_ADMIN}?status=pending")
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()["data"]]
    assert pending.id in ids
    assert approved.id not in ids


async def test_admin_pending_count(
    staff_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The pending-count badge counts only pending reviews."""
    product = await make_product(db_session, code="adm-count", slug="c")
    await _seed_pending(db_session, product_id=product.id, user_id=8600)
    await _seed_pending(db_session, product_id=product.id, user_id=8601)
    resp = await staff_client.get(f"{_ADMIN}/pending-count")
    assert resp.status_code == 200
    assert resp.json()["count"] == 2


async def test_admin_delete(
    staff_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Staff can hard-delete any review (204)."""
    product = await make_product(db_session, code="adm-del", slug="d")
    review = await _seed_pending(db_session, product_id=product.id, user_id=8700)
    resp = await staff_client.delete(f"{_ADMIN}/{review.id}")
    assert resp.status_code == 204
    assert await db_session.get(Review, review.id) is None


async def test_admin_guard_guest_401(guest_client: AsyncClient) -> None:
    """A guest is rejected by the staff guard with 401."""
    resp = await guest_client.get(_ADMIN)
    assert resp.status_code == 401


async def test_admin_guard_customer_403(client: AsyncClient) -> None:
    """A logged-in non-staff customer is rejected with 403."""
    resp = await client.get(_ADMIN)
    assert resp.status_code == 403
