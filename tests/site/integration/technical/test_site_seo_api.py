"""Tests for the public ``GET /site/seo`` endpoint (§6.4)."""

from httpx import AsyncClient


async def test_public_seo_returns_defaults_without_auth(
    public_client: AsyncClient,
) -> None:
    """A fresh install returns a fully-formed empty block, no token needed."""
    resp = await public_client.get("/api/v1/site/seo")
    assert resp.status_code == 200
    assert resp.json() == {
        "title_ru": "",
        "title_ro": "",
        "description_ru": "",
        "description_ro": "",
        "title_suffix": "",
        "og_image_url": "",
    }


async def test_public_seo_reflects_saved_defaults(
    public_client: AsyncClient,
    staff_client: AsyncClient,
) -> None:
    """Values saved via the admin PUT are readable on the public endpoint."""
    payload = {
        "title_ru": "evix — магазин",
        "title_ro": "evix — magazin",
        "description_ru": "Товары для дома и авто",
        "description_ro": "Produse pentru casă și auto",
        "title_suffix": " — evix",
        "og_image_url": "https://media.evix.md/public/og-default.jpg",
    }
    saved = await staff_client.put("/api/v1/admin/settings/seo", json=payload)
    assert saved.status_code == 200, saved.text

    public = await public_client.get("/api/v1/site/seo")
    assert public.status_code == 200
    assert public.json() == payload
