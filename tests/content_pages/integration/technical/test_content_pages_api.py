"""API tests for the content-pages surface (CMS-lite, Phase 1).

Covers the public read endpoints, the admin CRUD lifecycle (with public
visibility crossing over the shared session), input validation, and the
``current_staff`` guard on an admin route.
"""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


def _page_payload(
    slug: str = "about-us",
    *,
    is_published: bool = True,
    show_in_footer: bool = True,
    position: int = 0,
    title_ru: str = "О нас",
    title_ro: str = "Despre noi",
) -> dict:
    """Build a valid create/update payload with both languages."""
    return {
        "slug": slug,
        "is_published": is_published,
        "show_in_footer": show_in_footer,
        "position": position,
        "translations": [
            {
                "lang": "ru",
                "title": title_ru,
                "body": "# Тело ru",
                "seo_description": "seo ru",
            },
            {
                "lang": "ro",
                "title": title_ro,
                "body": "# Corp ro",
                "seo_description": None,
            },
        ],
    }


# --------------------------------------------------------------------------- #
# Public reads
# --------------------------------------------------------------------------- #
async def test_unpublished_page_hidden_and_detail_404(
    staff_client: AsyncClient,
    public_client: AsyncClient,
) -> None:
    """An unpublished page is absent from the footer list and 404s on detail."""
    created = await staff_client.post(
        "/api/v1/admin/content-pages",
        json=_page_payload(slug="draft-page", is_published=False),
    )
    assert created.status_code == 201, created.text

    listing = await public_client.get("/api/v1/site/pages?lang=ru")
    assert listing.status_code == 200
    assert all(item["slug"] != "draft-page" for item in listing.json())

    detail = await public_client.get("/api/v1/site/pages/draft-page?lang=ru")
    assert detail.status_code == 404


async def test_published_footer_page_listed_and_detail_returns_lang(
    staff_client: AsyncClient,
    public_client: AsyncClient,
) -> None:
    """A published footer page appears; detail returns the requested language."""
    resp = await staff_client.post(
        "/api/v1/admin/content-pages",
        json=_page_payload(slug="delivery", title_ru="Доставка", title_ro="Livrare"),
    )
    assert resp.status_code == 201, resp.text

    listing = await public_client.get("/api/v1/site/pages?lang=ro")
    assert listing.status_code == 200
    entries = {item["slug"]: item["title"] for item in listing.json()}
    assert entries["delivery"] == "Livrare"

    ru = await public_client.get("/api/v1/site/pages/delivery?lang=ru")
    assert ru.status_code == 200
    assert ru.json() == {
        "slug": "delivery",
        "title": "Доставка",
        "body": "# Тело ru",
        "seo_description": "seo ru",
    }


async def test_default_lang_is_ro(
    staff_client: AsyncClient,
    public_client: AsyncClient,
) -> None:
    """Omitting ``lang`` falls back to ``ro``."""
    resp = await staff_client.post(
        "/api/v1/admin/content-pages",
        json=_page_payload(slug="terms", title_ro="Termeni"),
    )
    assert resp.status_code == 201, resp.text

    detail = await public_client.get("/api/v1/site/pages/terms")
    assert detail.status_code == 200
    assert detail.json()["title"] == "Termeni"


async def test_missing_lang_detail_404(
    staff_client: AsyncClient,
    public_client: AsyncClient,
) -> None:
    """Requesting a language the page lacks 404s (only ru/ro exist)."""
    resp = await staff_client.post(
        "/api/v1/admin/content-pages",
        json=_page_payload(slug="privacy"),
    )
    assert resp.status_code == 201, resp.text

    detail = await public_client.get("/api/v1/site/pages/privacy?lang=en")
    assert detail.status_code == 404


async def test_footer_excludes_non_footer_pages(
    staff_client: AsyncClient,
    public_client: AsyncClient,
) -> None:
    """A published page with ``show_in_footer=False`` is not in the footer list."""
    resp = await staff_client.post(
        "/api/v1/admin/content-pages",
        json=_page_payload(slug="hidden-page", show_in_footer=False),
    )
    assert resp.status_code == 201, resp.text

    listing = await public_client.get("/api/v1/site/pages?lang=ru")
    assert all(item["slug"] != "hidden-page" for item in listing.json())


# --------------------------------------------------------------------------- #
# Admin CRUD lifecycle
# --------------------------------------------------------------------------- #
async def test_admin_crud_lifecycle(
    staff_client: AsyncClient,
    public_client: AsyncClient,
) -> None:
    """create → list → get → update (retitle + publish) → public → delete."""
    created = await staff_client.post(
        "/api/v1/admin/content-pages",
        json=_page_payload(slug="faq", is_published=False, title_ro="FAQ ro"),
    )
    assert created.status_code == 201, created.text
    page = created.json()
    page_id = page["id"]
    assert page["slug"] == "faq"
    assert page["is_published"] is False
    assert len(page["translations"]) == 2

    listing = await staff_client.get("/api/v1/admin/content-pages")
    assert listing.status_code == 200
    assert any(item["id"] == page_id for item in listing.json())

    got = await staff_client.get(f"/api/v1/admin/content-pages/{page_id}")
    assert got.status_code == 200
    assert got.json()["id"] == page_id

    # Not visible publicly while unpublished.
    hidden = await public_client.get("/api/v1/site/pages/faq?lang=ro")
    assert hidden.status_code == 404

    updated = await staff_client.put(
        f"/api/v1/admin/content-pages/{page_id}",
        json=_page_payload(slug="faq", is_published=True, title_ro="Întrebări"),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["is_published"] is True

    # Now visible with the new title.
    public_detail = await public_client.get("/api/v1/site/pages/faq?lang=ro")
    assert public_detail.status_code == 200
    assert public_detail.json()["title"] == "Întrebări"

    deleted = await staff_client.delete(f"/api/v1/admin/content-pages/{page_id}")
    assert deleted.status_code == 204

    gone = await staff_client.get(f"/api/v1/admin/content-pages/{page_id}")
    assert gone.status_code == 404


async def test_get_unknown_page_404(staff_client: AsyncClient) -> None:
    """Fetching a non-existent page id returns 404."""
    resp = await staff_client.get("/api/v1/admin/content-pages/999999")
    assert resp.status_code == 404


async def test_update_unknown_page_404(staff_client: AsyncClient) -> None:
    """Updating a non-existent page id returns 404."""
    resp = await staff_client.put(
        "/api/v1/admin/content-pages/999999",
        json=_page_payload(slug="whatever"),
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
async def test_bad_slug_422(staff_client: AsyncClient) -> None:
    """A slug that is not clean-hyphenated is rejected with 422."""
    resp = await staff_client.post(
        "/api/v1/admin/content-pages",
        json=_page_payload(slug="Bad Slug!"),
    )
    assert resp.status_code == 422


async def test_missing_one_lang_422(staff_client: AsyncClient) -> None:
    """Submitting only one language is rejected with 422."""
    payload = _page_payload(slug="mono")
    payload["translations"] = [payload["translations"][0]]  # ru only
    resp = await staff_client.post("/api/v1/admin/content-pages", json=payload)
    assert resp.status_code == 422


async def test_empty_body_422(staff_client: AsyncClient) -> None:
    """A blank body is rejected with 422."""
    payload = _page_payload(slug="blank")
    payload["translations"][0]["body"] = ""
    resp = await staff_client.post("/api/v1/admin/content-pages", json=payload)
    assert resp.status_code == 422


async def test_duplicate_slug_409(staff_client: AsyncClient) -> None:
    """Creating a second page with an existing slug returns 409."""
    first = await staff_client.post(
        "/api/v1/admin/content-pages",
        json=_page_payload(slug="contacts"),
    )
    assert first.status_code == 201, first.text

    dup = await staff_client.post(
        "/api/v1/admin/content-pages",
        json=_page_payload(slug="contacts"),
    )
    assert dup.status_code == 409


async def test_update_slug_clash_409(staff_client: AsyncClient) -> None:
    """Updating a page to a slug owned by another page returns 409."""
    a = await staff_client.post(
        "/api/v1/admin/content-pages",
        json=_page_payload(slug="page-a"),
    )
    assert a.status_code == 201, a.text
    b = await staff_client.post(
        "/api/v1/admin/content-pages",
        json=_page_payload(slug="page-b"),
    )
    assert b.status_code == 201, b.text

    clash = await staff_client.put(
        f"/api/v1/admin/content-pages/{b.json()['id']}",
        json=_page_payload(slug="page-a"),
    )
    assert clash.status_code == 409


# --------------------------------------------------------------------------- #
# Guard
# --------------------------------------------------------------------------- #
async def test_guard_blocks_anonymous(public_client: AsyncClient) -> None:
    """Without a token the real ``current_staff`` dependency yields 401."""
    resp = await public_client.get("/api/v1/admin/content-pages")
    assert resp.status_code == 401
