"""ProductOut.rating_* tests (Reviews & Ratings §3).

The PDP detail (``GET /catalog/products/{slug}``) must expose ``rating_avg`` /
``rating_count`` computed from approved reviews only — 0 / None without any, real
values once reviews are approved.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import STATUS_APPROVED, STATUS_PENDING, Review
from tests.reviews.conftest import make_product, make_user

pytestmark = pytest.mark.asyncio


async def _seed_review(
    db_session: AsyncSession,
    *,
    product_id: int,
    user_id: int,
    rating: int,
    status: str,
) -> None:
    """Persist a review with a given rating + status."""
    await make_user(db_session, user_id)
    db_session.add(
        Review(
            product_id=product_id,
            user_id=user_id,
            rating=rating,
            status=status,
            is_verified=False,
            lang="ru",
        )
    )
    await db_session.flush()


async def test_product_rating_none_without_reviews(
    guest_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A product with no approved reviews reports rating_avg None / count 0."""
    await make_product(db_session, code="pr-none", slug="prn")
    # A pending review must not count.
    product_resp = await guest_client.get(
        "/api/v1/catalog/products/prn-ru?lang=ru"
    )
    assert product_resp.status_code == 200
    body = product_resp.json()
    assert body["rating_avg"] is None
    assert body["rating_count"] == 0


async def test_product_rating_ignores_pending(
    guest_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Pending reviews do not contribute to the PDP rating summary."""
    product = await make_product(db_session, code="pr-pend", slug="prp")
    await _seed_review(
        db_session, product_id=product.id, user_id=8800, rating=1, status=STATUS_PENDING
    )
    resp = await guest_client.get("/api/v1/catalog/products/prp-ru?lang=ru")
    body = resp.json()
    assert body["rating_avg"] is None
    assert body["rating_count"] == 0


async def test_product_rating_after_approval(
    guest_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Approved reviews drive rating_avg (1 dp) and rating_count on the PDP."""
    product = await make_product(db_session, code="pr-appr", slug="pra")
    # Approved 5 + 4 -> avg 4.5, count 2.
    await _seed_review(
        db_session, product_id=product.id, user_id=8900, rating=5, status=STATUS_APPROVED
    )
    await _seed_review(
        db_session, product_id=product.id, user_id=8901, rating=4, status=STATUS_APPROVED
    )
    resp = await guest_client.get("/api/v1/catalog/products/pra-ru?lang=ru")
    body = resp.json()
    assert body["rating_avg"] == 4.5
    assert body["rating_count"] == 2
