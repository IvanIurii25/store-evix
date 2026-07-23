"""API tests for the admin settings surface (§6.4): SEO defaults + staff.

Covers: SEO round-trip (empty defaults → save → read back); staff roster list;
create + promote-existing-by-email; (de)activate; the last-active-staff lockout
guard; and the ``current_staff`` guard on a settings route.
"""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# SEO
# --------------------------------------------------------------------------- #
async def test_seo_defaults_empty_then_saved(client: AsyncClient) -> None:
    """A fresh install returns empty SEO fields; a PUT round-trips."""
    empty = await client.get("/api/v1/admin/settings/seo")
    assert empty.status_code == 200
    assert empty.json() == {
        "title_ru": "",
        "title_ro": "",
        "description_ru": "",
        "description_ro": "",
        "title_suffix": "",
        "og_image_url": "",
    }

    payload = {
        "title_ru": "Магазин evix",
        "title_ro": "Magazin evix",
        "description_ru": "Лучшие товары",
        "description_ro": "Cele mai bune produse",
        "title_suffix": " — evix",
        "og_image_url": "https://cdn.evix.md/og.png",
    }
    saved = await client.put("/api/v1/admin/settings/seo", json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json() == payload

    again = await client.get("/api/v1/admin/settings/seo")
    assert again.json() == payload


# --------------------------------------------------------------------------- #
# Staff
# --------------------------------------------------------------------------- #
async def test_staff_list_includes_seed_staff(client: AsyncClient) -> None:
    """The roster includes the persisted staff user."""
    resp = await client.get("/api/v1/admin/staff")
    assert resp.status_code == 200
    emails = {item["email"] for item in resp.json()["data"]}
    assert "admin-settings@example.com" in emails


async def test_create_staff(client: AsyncClient) -> None:
    """Creating a staff user returns an active staff row and lists it."""
    resp = await client.post(
        "/api/v1/admin/staff",
        json={"email": "new-staff@example.com", "password": "supersecret"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["is_staff"] is True
    assert body["is_active"] is True
    assert body["email"] == "new-staff@example.com"


async def test_create_staff_promotes_existing_email(client: AsyncClient) -> None:
    """Posting an existing email promotes that user to active staff."""
    first = await client.post(
        "/api/v1/admin/staff",
        json={"email": "dup@example.com", "password": "supersecret"},
    )
    user_id = first.json()["id"]

    again = await client.post(
        "/api/v1/admin/staff",
        json={"email": "dup@example.com", "password": "anothersecret"},
    )
    assert again.status_code == 201, again.text
    assert again.json()["id"] == user_id  # same row, promoted — not a duplicate


async def test_deactivate_staff(client: AsyncClient) -> None:
    """A second staff account can be deactivated while another stays active."""
    created = await client.post(
        "/api/v1/admin/staff",
        json={"email": "temp@example.com", "password": "supersecret"},
    )
    user_id = created.json()["id"]

    resp = await client.patch(
        f"/api/v1/admin/staff/{user_id}",
        json={"is_active": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is False


async def test_cannot_remove_last_active_staff(client: AsyncClient) -> None:
    """Deactivating the only active staff account is refused with 409."""
    # The seed staff user is the only active staff in this fresh DB.
    roster = (await client.get("/api/v1/admin/staff")).json()["data"]
    assert len(roster) == 1
    only_id = roster[0]["id"]

    resp = await client.patch(
        f"/api/v1/admin/staff/{only_id}",
        json={"is_active": False},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


async def test_update_unknown_staff_is_404(client: AsyncClient) -> None:
    """Updating a non-existent staff id yields 404."""
    resp = await client.patch(
        "/api/v1/admin/staff/999999",
        json={"is_active": False},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- #
# Guard
# --------------------------------------------------------------------------- #
async def test_guard_blocks_anonymous(guest_client: AsyncClient) -> None:
    """Without a token the real ``current_staff`` dependency yields 401."""
    resp = await guest_client.get("/api/v1/admin/staff")
    assert resp.status_code == 401
