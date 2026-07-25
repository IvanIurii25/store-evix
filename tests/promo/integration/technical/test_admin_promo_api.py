"""API tests for the admin promo-code CRUD surface (feature A1, §2.5).

Covers: create → list → get round-trip; partial PATCH; DELETE; duplicate-code
conflict (409); invalid percent > 100 (422); not-found (404) on get/patch/delete;
and the ``current_staff`` guard (403) on the roster.
"""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

_PROMO = "/api/v1/admin/promo"

_VALID_BODY = {
    "code": "WELCOME10",
    "discount_type": "percent",
    "discount_value": "10",
    "active_from": "2026-01-01T00:00:00Z",
    "active_to": "2026-12-31T23:59:59Z",
    "min_order_total": "50",
    "usage_limit": 100,
    "is_active": True,
}


async def test_create_then_get_and_list(client: AsyncClient) -> None:
    """Creating a coupon returns it; it is then fetchable and listed."""
    created = await client.post(_PROMO, json=_VALID_BODY)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["code"] == "WELCOME10"
    promo_id = body["id"]

    got = await client.get(f"{_PROMO}/{promo_id}")
    assert got.status_code == 200
    assert got.json()["code"] == "WELCOME10"

    listed = await client.get(_PROMO)
    assert listed.status_code == 200
    codes = {item["code"] for item in listed.json()["data"]}
    assert "WELCOME10" in codes


async def test_patch_updates_fields(client: AsyncClient) -> None:
    """A partial PATCH changes only the supplied fields."""
    created = await client.post(_PROMO, json=_VALID_BODY)
    promo_id = created.json()["id"]

    patched = await client.patch(
        f"{_PROMO}/{promo_id}", json={"discount_value": "25", "is_active": False}
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["discount_value"] == "25.00"
    assert body["is_active"] is False
    assert body["code"] == "WELCOME10"  # untouched


async def test_delete_removes_promo(client: AsyncClient) -> None:
    """DELETE removes the coupon (subsequent GET is 404)."""
    created = await client.post(_PROMO, json=_VALID_BODY)
    promo_id = created.json()["id"]

    deleted = await client.delete(f"{_PROMO}/{promo_id}")
    assert deleted.status_code == 204, deleted.text

    got = await client.get(f"{_PROMO}/{promo_id}")
    assert got.status_code == 404


async def test_duplicate_code_conflict(client: AsyncClient) -> None:
    """A second coupon with the same code is a 409 ``conflict``."""
    first = await client.post(_PROMO, json=_VALID_BODY)
    assert first.status_code == 201

    dup = await client.post(_PROMO, json=_VALID_BODY)
    assert dup.status_code == 409, dup.text
    assert dup.json()["error"]["code"] == "conflict"


async def test_percent_over_100_rejected(client: AsyncClient) -> None:
    """A percent discount above 100 is rejected (422 ``validation_error``)."""
    body = {**_VALID_BODY, "code": "TOOMUCH", "discount_value": "150"}
    resp = await client.post(_PROMO, json=body)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "validation_error"


async def test_get_missing_404(client: AsyncClient) -> None:
    """Fetching a non-existent coupon returns ``not_found`` (404)."""
    resp = await client.get(f"{_PROMO}/999999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_patch_missing_404(client: AsyncClient) -> None:
    """Patching a non-existent coupon returns 404."""
    resp = await client.patch(f"{_PROMO}/999999", json={"is_active": False})
    assert resp.status_code == 404


async def test_delete_missing_404(client: AsyncClient) -> None:
    """Deleting a non-existent coupon returns 404."""
    resp = await client.delete(f"{_PROMO}/999999")
    assert resp.status_code == 404


async def test_staff_guard_blocks_guest(guest_client: AsyncClient) -> None:
    """A non-authenticated caller is blocked by the ``current_staff`` guard."""
    resp = await guest_client.get(_PROMO)
    assert resp.status_code == 401
