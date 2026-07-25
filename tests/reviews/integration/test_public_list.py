"""Public review list + aggregate tests (Reviews & Ratings §7).

The public list (``GET /catalog/products/{slug}/reviews``) must return only
``approved`` reviews and the correct aggregate (avg / count / per-star
distribution), and must resolve the product by its localized slug.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    Review,
)
from tests.reviews.conftest import make_product, make_user

pytestmark = pytest.mark.asyncio


async def _seed_review(
    db_session: AsyncSession,
    *,
    product_id: int,
    user_id: int,
    rating: int,
    status: str,
) -> Review:
    """Persist a review row with a given rating + status for a distinct user."""
    await make_user(db_session, user_id)
    review = Review(
        product_id=product_id,
        user_id=user_id,
        rating=rating,
        status=status,
        is_verified=False,
        lang="ru",
    )
    db_session.add(review)
    await db_session.flush()
    return review


async def test_public_list_only_approved(
    guest_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Only approved reviews appear publicly; pending/rejected are hidden."""
    product = await make_product(db_session, code="pub-appr", slug="lamp")
    await _seed_review(
        db_session, product_id=product.id, user_id=8001, rating=5, status=STATUS_APPROVED
    )
    await _seed_review(
        db_session, product_id=product.id, user_id=8002, rating=1, status=STATUS_PENDING
    )
    await _seed_review(
        db_session, product_id=product.id, user_id=8003, rating=2, status=STATUS_REJECTED
    )

    resp = await guest_client.get(
        "/api/v1/catalog/products/lamp-ru/reviews?lang=ru"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["rating"] == 5


async def test_public_aggregate(
    guest_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Aggregate reflects only approved reviews: avg (1 dp), count, distribution."""
    product = await make_product(db_session, code="pub-agg", slug="chair")
    # Approved ratings: 5, 4, 3 -> avg 4.0, count 3.
    for idx, rating in enumerate((5, 4, 3)):
        await _seed_review(
            db_session,
            product_id=product.id,
            user_id=8100 + idx,
            rating=rating,
            status=STATUS_APPROVED,
        )
    # A pending 1-star must not affect the aggregate.
    await _seed_review(
        db_session, product_id=product.id, user_id=8199, rating=1, status=STATUS_PENDING
    )

    resp = await guest_client.get(
        "/api/v1/catalog/products/chair-ru/reviews?lang=ru"
    )
    assert resp.status_code == 200
    agg = resp.json()["aggregate"]
    assert agg["average"] == 4.0
    assert agg["count"] == 3
    # Distribution keys come back as strings over JSON.
    assert agg["distribution"]["5"] == 1
    assert agg["distribution"]["4"] == 1
    assert agg["distribution"]["3"] == 1
    assert agg["distribution"]["2"] == 0
    assert agg["distribution"]["1"] == 0


async def test_public_aggregate_empty(
    guest_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """With no approved reviews the aggregate is avg None / count 0."""
    product = await make_product(db_session, code="pub-empty", slug="empty")
    await _seed_review(
        db_session, product_id=product.id, user_id=8200, rating=5, status=STATUS_PENDING
    )
    resp = await guest_client.get(
        "/api/v1/catalog/products/empty-ru/reviews?lang=ru"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["aggregate"]["average"] is None
    assert body["aggregate"]["count"] == 0


async def test_public_sort_highest_lowest(
    guest_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The ``highest`` / ``lowest`` sort orders the approved reviews by rating."""
    product = await make_product(db_session, code="pub-sort", slug="sortp")
    for idx, rating in enumerate((2, 5, 3)):
        await _seed_review(
            db_session,
            product_id=product.id,
            user_id=8300 + idx,
            rating=rating,
            status=STATUS_APPROVED,
        )

    highest = await guest_client.get(
        "/api/v1/catalog/products/sortp-ru/reviews?lang=ru&sort=highest"
    )
    ratings = [r["rating"] for r in highest.json()["data"]]
    assert ratings == [5, 3, 2]

    lowest = await guest_client.get(
        "/api/v1/catalog/products/sortp-ru/reviews?lang=ru&sort=lowest"
    )
    ratings = [r["rating"] for r in lowest.json()["data"]]
    assert ratings == [2, 3, 5]


async def test_public_unknown_slug_404(guest_client: AsyncClient) -> None:
    """An unknown product slug returns 404."""
    resp = await guest_client.get(
        "/api/v1/catalog/products/does-not-exist/reviews?lang=ru"
    )
    assert resp.status_code == 404
