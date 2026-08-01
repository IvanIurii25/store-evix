"""Banner API tests: the public carousel read and the back-office CRUD (P0)."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.banners.conftest import banner_payload

PUBLIC = "/api/v1/site/banners"
ADMIN = "/api/v1/admin/banners"


# --------------------------------------------------------------------------- #
# Public read
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_public_returns_active_banner_for_the_language(staff_client):
    """A live banner is served with the creative of the requested language."""
    created = await staff_client.post(ADMIN, json=banner_payload())
    assert created.status_code == 201

    ru = await staff_client.get(PUBLIC, params={"lang": "ru"})
    ro = await staff_client.get(PUBLIC, params={"lang": "ro"})

    assert ru.status_code == 200
    assert [b["alt"] for b in ru.json()] == ["Летняя распродажа"]
    assert ru.json()[0]["image_url"].endswith("banner-ru.jpg")
    assert ru.json()[0]["link_url"] == "/ru/c/dom"
    # The romanian creative has no mobile art — the field is present and null,
    # so the storefront can decide without probing.
    assert ro.json()[0]["image_mobile_url"] is None
    assert [b["alt"] for b in ro.json()] == ["Reduceri de vară"]


@pytest.mark.asyncio
async def test_public_hides_inactive_banners(staff_client):
    """An inactive banner is invisible even though it is fully filled in."""
    await staff_client.post(ADMIN, json=banner_payload(is_active=False))

    response = await staff_client.get(PUBLIC, params={"lang": "ru"})

    assert response.json() == []


@pytest.mark.asyncio
async def test_public_respects_the_display_window(staff_client):
    """Banners outside their schedule stay hidden; an open end means open-ended."""
    now = datetime.now(UTC)
    future = await staff_client.post(
        ADMIN,
        json=banner_payload(
            starts_at=(now + timedelta(days=1)).isoformat(),
            translations=banner_payload()["translations"],
        ),
    )
    expired = await staff_client.post(
        ADMIN,
        json=banner_payload(ends_at=(now - timedelta(days=1)).isoformat()),
    )
    open_ended = await staff_client.post(ADMIN, json=banner_payload(position=5))
    assert {future.status_code, expired.status_code, open_ended.status_code} == {201}

    live = (await staff_client.get(PUBLIC, params={"lang": "ru"})).json()

    assert [b["id"] for b in live] == [open_ended.json()["id"]]


@pytest.mark.asyncio
async def test_public_orders_by_position(staff_client):
    """Slides come back in the order the manager arranged, not by id."""
    first = await staff_client.post(ADMIN, json=banner_payload(position=10))
    second = await staff_client.post(ADMIN, json=banner_payload(position=1))

    live = (await staff_client.get(PUBLIC, params={"lang": "ru"})).json()

    assert [b["id"] for b in live] == [second.json()["id"], first.json()["id"]]


@pytest.mark.asyncio
async def test_public_empty_is_a_normal_answer(public_client):
    """No banners is an empty list, not an error — the storefront falls back."""
    response = await public_client.get(PUBLIC, params={"lang": "ru"})

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_public_never_leaks_the_schedule(staff_client):
    """The public payload carries no scheduling or admin-only fields."""
    await staff_client.post(ADMIN, json=banner_payload())

    slide = (await staff_client.get(PUBLIC, params={"lang": "ru"})).json()[0]

    assert set(slide) == {
        "id",
        "image_url",
        "image_mobile_url",
        "alt",
        "title",
        "subtitle",
        "cta_label",
        "link_url",
    }


# --------------------------------------------------------------------------- #
# Admin CRUD
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_admin_requires_staff(public_client):
    """Every admin route sits behind the staff guard."""
    listed = await public_client.get(ADMIN)
    created = await public_client.post(ADMIN, json=banner_payload())

    assert listed.status_code in (401, 403)
    assert created.status_code in (401, 403)


@pytest.mark.asyncio
async def test_admin_lists_banners_in_any_state(staff_client):
    """The back-office sees expired and inactive banners the storefront hides."""
    await staff_client.post(ADMIN, json=banner_payload(is_active=False))

    listed = (await staff_client.get(ADMIN)).json()

    assert len(listed) == 1
    assert listed[0]["is_active"] is False
    assert {tr["lang"] for tr in listed[0]["translations"]} == {"ru", "ro"}


@pytest.mark.asyncio
async def test_admin_update_replaces_both_creatives(staff_client):
    """A full update swaps the creatives rather than merging them."""
    banner_id = (await staff_client.post(ADMIN, json=banner_payload())).json()["id"]
    payload = banner_payload(position=3, is_active=False)
    payload["translations"][0]["image_url"] = "https://media.evix.md/media/new-ru.jpg"
    payload["translations"][0]["alt"] = "Новый баннер"

    updated = await staff_client.put(f"{ADMIN}/{banner_id}", json=payload)

    assert updated.status_code == 200
    body = updated.json()
    assert body["position"] == 3 and body["is_active"] is False
    ru = next(tr for tr in body["translations"] if tr["lang"] == "ru")
    assert ru["image_url"].endswith("new-ru.jpg")
    assert ru["alt"] == "Новый баннер"
    # Still exactly two rows — the replaced translation did not pile up.
    assert len(body["translations"]) == 2


@pytest.mark.asyncio
async def test_admin_delete_removes_the_banner(staff_client):
    """Deleting drops the banner and its creatives."""
    banner_id = (await staff_client.post(ADMIN, json=banner_payload())).json()["id"]

    deleted = await staff_client.delete(f"{ADMIN}/{banner_id}")
    missing = await staff_client.get(f"{ADMIN}/{banner_id}")

    assert deleted.status_code == 204
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_admin_reorder_applies_positions(staff_client):
    """Reorder writes the new display order in one call."""
    first = (await staff_client.post(ADMIN, json=banner_payload(position=0))).json()
    second = (await staff_client.post(ADMIN, json=banner_payload(position=1))).json()

    response = await staff_client.post(
        f"{ADMIN}/reorder",
        json={
            "items": [
                {"banner_id": first["id"], "position": 9},
                {"banner_id": second["id"], "position": 2},
            ]
        },
    )

    assert response.status_code == 200
    assert [b["id"] for b in response.json()] == [second["id"], first["id"]]


@pytest.mark.asyncio
async def test_admin_reorder_refuses_unknown_ids(staff_client):
    """A stale editor cannot silently drop a banner out of the order."""
    banner = (await staff_client.post(ADMIN, json=banner_payload())).json()

    response = await staff_client.post(
        f"{ADMIN}/reorder",
        json={
            "items": [
                {"banner_id": banner["id"], "position": 1},
                {"banner_id": 999_999, "position": 2},
            ]
        },
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_admin_unknown_banner_is_404(staff_client):
    """Reads, updates and deletes of a missing banner answer 404."""
    assert (await staff_client.get(f"{ADMIN}/999999")).status_code == 404
    assert (
        await staff_client.put(f"{ADMIN}/999999", json=banner_payload())
    ).status_code == 404
    assert (await staff_client.delete(f"{ADMIN}/999999")).status_code == 404


# --------------------------------------------------------------------------- #
# Per-language link
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_each_language_gets_its_own_link(staff_client):
    """Storefront paths carry the locale, so the link is per language."""
    payload = banner_payload(link_url=None)
    payload["translations"][0]["link_url"] = "/ru/p/chasy"
    payload["translations"][1]["link_url"] = "/ro/p/ceas"
    await staff_client.post(ADMIN, json=payload)

    ru = (await staff_client.get(PUBLIC, params={"lang": "ru"})).json()[0]
    ro = (await staff_client.get(PUBLIC, params={"lang": "ro"})).json()[0]

    assert ru["link_url"] == "/ru/p/chasy"
    assert ro["link_url"] == "/ro/p/ceas"


@pytest.mark.asyncio
async def test_banner_link_is_the_fallback(staff_client):
    """A language without its own link falls back to the banner-level one.

    That is what keeps banners created before this field from breaking.
    """
    payload = banner_payload(link_url="/ro/c/dom")
    payload["translations"][0]["link_url"] = "/ru/c/dom"
    # ro deliberately left without its own link
    await staff_client.post(ADMIN, json=payload)

    ru = (await staff_client.get(PUBLIC, params={"lang": "ru"})).json()[0]
    ro = (await staff_client.get(PUBLIC, params={"lang": "ro"})).json()[0]

    assert ru["link_url"] == "/ru/c/dom"
    assert ro["link_url"] == "/ro/c/dom"


@pytest.mark.asyncio
async def test_translation_link_survives_a_round_trip(staff_client):
    """The back-office reads back what it saved, per language."""
    payload = banner_payload()
    payload["translations"][0]["link_url"] = "/ru/p/chasy"
    banner_id = (await staff_client.post(ADMIN, json=payload)).json()["id"]

    body = (await staff_client.get(f"{ADMIN}/{banner_id}")).json()
    ru = next(t for t in body["translations"] if t["lang"] == "ru")
    ro = next(t for t in body["translations"] if t["lang"] == "ro")

    assert ru["link_url"] == "/ru/p/chasy"
    assert ro["link_url"] is None
