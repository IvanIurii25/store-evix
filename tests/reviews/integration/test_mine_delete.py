"""Own-review tests: GET mine + author-only DELETE (Reviews & Ratings §7)."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.reviews.conftest import make_product

pytestmark = pytest.mark.asyncio

_URL = "/api/v1/reviews"


async def test_get_mine_returns_own_pending(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """GET mine returns the caller's own review even while it is pending."""
    product = await make_product(db_session, code="mine-1")
    await client.post(
        _URL, json={"product_id": product.id, "rating": 4, "lang": "ru"}
    )
    resp = await client.get(f"{_URL}/mine/{product.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rating"] == 4
    assert body["status"] == "pending"


async def test_get_mine_none_when_absent(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """GET mine returns ``null`` when the caller has no review."""
    product = await make_product(db_session, code="mine-none")
    resp = await client.get(f"{_URL}/mine/{product.id}")
    assert resp.status_code == 200
    assert resp.json() is None


async def test_delete_own_review(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The author can delete their own review (204, then gone)."""
    product = await make_product(db_session, code="del-own")
    created = await client.post(
        _URL, json={"product_id": product.id, "rating": 3, "lang": "ru"}
    )
    review_id = created.json()["id"]
    resp = await client.delete(f"{_URL}/{review_id}")
    assert resp.status_code == 204
    assert (await client.get(f"{_URL}/mine/{product.id}")).json() is None


async def test_delete_other_users_review_forbidden(
    client: AsyncClient,
    other_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A non-author gets 403 when deleting someone else's review."""
    product = await make_product(db_session, code="del-other")
    created = await client.post(
        _URL, json={"product_id": product.id, "rating": 3, "lang": "ru"}
    )
    review_id = created.json()["id"]
    resp = await other_client.delete(f"{_URL}/{review_id}")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


async def test_delete_missing_review_404(client: AsyncClient) -> None:
    """Deleting a non-existent review returns 404."""
    resp = await client.delete(f"{_URL}/999999")
    assert resp.status_code == 404


async def test_delete_guest_401(
    guest_client: AsyncClient,
) -> None:
    """A guest cannot delete (401 from ``current_user``)."""
    resp = await guest_client.delete(f"{_URL}/1")
    assert resp.status_code == 401
