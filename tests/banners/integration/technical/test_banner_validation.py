"""Banner payload validation: the edge rules that keep bad data out (P0)."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.banners.conftest import banner_payload

ADMIN = "/api/v1/admin/banners"


@pytest.mark.parametrize(
    "link",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "http://example.com/promo",
        "//evil.example/promo",
        "vbscript:msgbox(1)",
    ],
)
@pytest.mark.asyncio
async def test_rejects_unsafe_or_downgrading_links(staff_client, link):
    """A banner link is staff-typed and rendered site-wide — only path or https.

    The scheme cases are the XSS vector; ``http://`` would downgrade the
    connection and ``//host`` is an external link wearing a path's clothes.
    """
    response = await staff_client.post(ADMIN, json=banner_payload(link_url=link))

    assert response.status_code == 422


@pytest.mark.parametrize(
    "link",
    ["/ru/c/dom", "/ro/p/aspirator", "https://evix.md/promo", None],
)
@pytest.mark.asyncio
async def test_accepts_internal_paths_and_https(staff_client, link):
    """Internal paths, https URLs and "no link at all" are all valid."""
    response = await staff_client.post(ADMIN, json=banner_payload(link_url=link))

    assert response.status_code == 201
    assert response.json()["link_url"] == link


@pytest.mark.asyncio
async def test_requires_both_languages(staff_client):
    """One language is refused: a visitor must never meet a half-translated carousel."""
    payload = banner_payload()
    payload["translations"] = [payload["translations"][0]]

    response = await staff_client.post(ADMIN, json=payload)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_rejects_duplicate_language(staff_client):
    """Two creatives for the same language would make the shown one arbitrary."""
    payload = banner_payload()
    payload["translations"][1]["lang"] = "ru"

    response = await staff_client.post(ADMIN, json=payload)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_requires_alt_text(staff_client):
    """``alt`` is what a screen reader announces, so an empty one is refused."""
    payload = banner_payload()
    payload["translations"][0]["alt"] = "   "

    response = await staff_client.post(ADMIN, json=payload)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_requires_a_desktop_creative(staff_client):
    """A banner without its main image has nothing to render."""
    payload = banner_payload()
    payload["translations"][0]["image_url"] = ""

    response = await staff_client.post(ADMIN, json=payload)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_rejects_a_window_that_can_never_open(staff_client):
    """``ends_at`` before ``starts_at`` would create a permanently hidden banner."""
    now = datetime.now(UTC)

    response = await staff_client.post(
        ADMIN,
        json=banner_payload(
            starts_at=now.isoformat(),
            ends_at=(now - timedelta(hours=1)).isoformat(),
        ),
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_blank_copy_is_stored_as_absent(staff_client):
    """An empty form field means "no overlay", not an empty heading in the DOM."""
    payload = banner_payload()
    payload["translations"][0]["title"] = "   "
    payload["translations"][0]["subtitle"] = ""

    created = await staff_client.post(ADMIN, json=payload)

    assert created.status_code == 201
    ru = next(tr for tr in created.json()["translations"] if tr["lang"] == "ru")
    assert ru["title"] is None
    assert ru["subtitle"] is None


@pytest.mark.asyncio
async def test_optional_copy_round_trips(staff_client):
    """Filled-in copy reaches the storefront, so overlay banners work."""
    payload = banner_payload()
    payload["translations"][0]["title"] = "Летняя распродажа"
    payload["translations"][0]["subtitle"] = "Скидки до 50%"
    payload["translations"][0]["cta_label"] = "Смотреть"

    await staff_client.post(ADMIN, json=payload)
    slide = (
        await staff_client.get("/api/v1/site/banners", params={"lang": "ru"})
    ).json()[0]

    assert slide["title"] == "Летняя распродажа"
    assert slide["subtitle"] == "Скидки до 50%"
    assert slide["cta_label"] == "Смотреть"
