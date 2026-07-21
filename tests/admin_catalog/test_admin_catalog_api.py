"""API tests for the thin catalog admin-API (B7, §10).

Covers: category create + translations; ``move`` recomputing ``path``/``depth``
for a whole subtree; product create + activation (one language rejected, both
languages accepted → cards rebuilt); media upload writing a file + a row;
attribute CRUD; back-office product search; and the ``current_staff`` guard.
"""

from io import BytesIO

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Category, ProductCard

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _cat_payload(name: str, slug: str, parent_id: int | None = None) -> dict:
    """Build a two-language category create payload."""
    return {
        "parent_id": parent_id,
        "is_active": False,
        "translations": [
            {"lang": "ru", "name": f"{name}-ru", "slug": f"{slug}-ru"},
            {"lang": "ro", "name": f"{name}-ro", "slug": f"{slug}-ro"},
        ],
    }


def _product_translations(slug: str, langs: tuple[str, ...]) -> list[dict]:
    """Build product translation payloads for the requested languages."""
    return [
        {"lang": lang, "name": f"phone-{lang}", "slug": f"{slug}-{lang}"}
        for lang in langs
    ]


# --------------------------------------------------------------------------- #
# Categories
# --------------------------------------------------------------------------- #
async def test_create_category_with_translations(client: AsyncClient) -> None:
    """A category is created with its path/depth and both translations."""
    response = await client.post(
        "/api/v1/admin/categories", json=_cat_payload("Root", "root")
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["parent_id"] is None
    assert body["depth"] == 0
    assert body["path"] == [body["id"]]
    langs = {tr["lang"] for tr in body["translations"]}
    assert langs == {"ru", "ro"}
    assert body["cover_image_url"] is None


async def test_update_category_cover_image(client: AsyncClient) -> None:
    """cover_image_url is set via PATCH, echoed back, and persisted."""
    created = (
        await client.post("/api/v1/admin/categories", json=_cat_payload("Root", "root"))
    ).json()

    resp = await client.patch(
        f"/api/v1/admin/categories/{created['id']}",
        json={"cover_image_url": "http://media/cat.jpg"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cover_image_url"] == "http://media/cat.jpg"

    listing = (await client.get("/api/v1/admin/categories")).json()
    row = next(c for c in listing if c["id"] == created["id"])
    assert row["cover_image_url"] == "http://media/cat.jpg"


async def test_move_recomputes_subtree_path_and_depth(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Moving a node rewrites path/depth for the node AND all descendants."""
    # Build a 3-level chain: root -> mid -> leaf, plus a second root.
    root = (
        await client.post("/api/v1/admin/categories", json=_cat_payload("R", "r"))
    ).json()
    mid = (
        await client.post(
            "/api/v1/admin/categories",
            json=_cat_payload("M", "m", parent_id=root["id"]),
        )
    ).json()
    leaf = (
        await client.post(
            "/api/v1/admin/categories",
            json=_cat_payload("L", "l", parent_id=mid["id"]),
        )
    ).json()
    other = (
        await client.post("/api/v1/admin/categories", json=_cat_payload("O", "o"))
    ).json()

    assert mid["path"] == [root["id"], mid["id"]]
    assert leaf["path"] == [root["id"], mid["id"], leaf["id"]]

    # Move ``mid`` (with its ``leaf`` subtree) under ``other``.
    moved = await client.post(
        f"/api/v1/admin/categories/{mid['id']}/move",
        json={"parent_id": other["id"]},
    )
    assert moved.status_code == 200, moved.text
    moved_body = moved.json()
    assert moved_body["parent_id"] == other["id"]
    assert moved_body["path"] == [other["id"], mid["id"]]
    assert moved_body["depth"] == 1

    # The descendant leaf must be rewritten too.
    leaf_row = await db_session.get(Category, leaf["id"])
    await db_session.refresh(leaf_row)
    assert leaf_row.path == [other["id"], mid["id"], leaf["id"]]
    assert leaf_row.depth == 2

    # And ``mid`` itself in the DB.
    mid_row = await db_session.get(Category, mid["id"])
    await db_session.refresh(mid_row)
    assert mid_row.path == [other["id"], mid["id"]]
    assert mid_row.depth == 1


async def test_move_to_root_clears_prefix(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Moving a nested node to root yields a single-element path, depth 0."""
    root = (
        await client.post("/api/v1/admin/categories", json=_cat_payload("R", "r"))
    ).json()
    child = (
        await client.post(
            "/api/v1/admin/categories",
            json=_cat_payload("C", "c", parent_id=root["id"]),
        )
    ).json()

    moved = await client.post(
        f"/api/v1/admin/categories/{child['id']}/move",
        json={"parent_id": None},
    )
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["path"] == [child["id"]]
    assert body["depth"] == 0
    assert body["parent_id"] is None


async def test_move_under_own_subtree_rejected(client: AsyncClient) -> None:
    """Re-parenting a node under its own descendant is a domain error."""
    root = (
        await client.post("/api/v1/admin/categories", json=_cat_payload("R", "r"))
    ).json()
    child = (
        await client.post(
            "/api/v1/admin/categories",
            json=_cat_payload("C", "c", parent_id=root["id"]),
        )
    ).json()

    response = await client.post(
        f"/api/v1/admin/categories/{root['id']}/move",
        json={"parent_id": child["id"]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid"


# --------------------------------------------------------------------------- #
# Products
# --------------------------------------------------------------------------- #
async def _make_category(client: AsyncClient) -> int:
    """Create a category and return its id."""
    return (
        await client.post("/api/v1/admin/categories", json=_cat_payload("Cat", "cat"))
    ).json()["id"]


async def test_activate_with_one_language_rejected(client: AsyncClient) -> None:
    """Activating a product with a single translation violates §2.1.1."""
    category_id = await _make_category(client)
    product = await client.post(
        "/api/v1/admin/products",
        json={
            "category_id": category_id,
            "code": "SKU-1",
            "price": "10.00",
            "qty": 5,
            "is_active": False,
            "translations": _product_translations("phone1", ("ru",)),
        },
    )
    assert product.status_code == 201, product.text
    product_id = product.json()["id"]

    response = await client.patch(
        f"/api/v1/admin/products/{product_id}", json={"is_active": True}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_publishable"


async def test_activate_with_both_languages_rebuilds_cards(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Activation with both translations succeeds and writes two card rows."""
    category_id = await _make_category(client)
    product = await client.post(
        "/api/v1/admin/products",
        json={
            "category_id": category_id,
            "code": "SKU-2",
            "price": "20.00",
            "qty": 3,
            "is_active": False,
            "translations": _product_translations("phone2", ("ru", "ro")),
        },
    )
    product_id = product.json()["id"]

    response = await client.patch(
        f"/api/v1/admin/products/{product_id}", json={"is_active": True}
    )
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is True

    cards = (
        (
            await db_session.execute(
                select(ProductCard).where(ProductCard.product_id == product_id)
            )
        )
        .scalars()
        .all()
    )
    assert {card.lang for card in cards} == {"ru", "ro"}
    assert all(card.is_active for card in cards)


async def test_create_active_product_directly(client: AsyncClient) -> None:
    """A product may be created active when both translations are supplied."""
    category_id = await _make_category(client)
    response = await client.post(
        "/api/v1/admin/products",
        json={
            "category_id": category_id,
            "code": "SKU-3",
            "price": "30.00",
            "qty": 1,
            "is_active": True,
            "translations": _product_translations("phone3", ("ru", "ro")),
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["is_active"] is True


async def test_search_products_by_code_and_name(client: AsyncClient) -> None:
    """Back-office search matches on ``code`` and on translated name."""
    category_id = await _make_category(client)
    await client.post(
        "/api/v1/admin/products",
        json={
            "category_id": category_id,
            "code": "ALPHA-100",
            "price": "5.00",
            "translations": _product_translations("alpha", ("ru", "ro")),
        },
    )
    await client.post(
        "/api/v1/admin/products",
        json={
            "category_id": category_id,
            "code": "BETA-200",
            "price": "6.00",
            "translations": _product_translations("beta", ("ru", "ro")),
        },
    )

    by_code = await client.get("/api/v1/admin/products", params={"search": "ALPHA"})
    assert by_code.status_code == 200
    codes = {item["code"] for item in by_code.json()["data"]}
    assert "ALPHA-100" in codes and "BETA-200" not in codes

    by_name = await client.get("/api/v1/admin/products", params={"search": "phone-ru"})
    assert by_name.status_code == 200
    assert len(by_name.json()["data"]) >= 2


async def test_upload_media_creates_file_and_row(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path,
    monkeypatch,
) -> None:
    """Media upload writes a file under media_root and records a Media row."""
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "storage_backend", "local")
    monkeypatch.setattr(cfg.settings, "media_root", str(tmp_path))
    monkeypatch.setattr(cfg.settings, "media_url_prefix", "/media")

    category_id = await _make_category(client)
    product = await client.post(
        "/api/v1/admin/products",
        json={
            "category_id": category_id,
            "code": "SKU-IMG",
            "price": "9.00",
            "translations": _product_translations("img", ("ru", "ro")),
        },
    )
    product_id = product.json()["id"]

    payload = BytesIO(b"\x89PNG\r\n\x1a\nfake-bytes")
    response = await client.post(
        f"/api/v1/admin/products/{product_id}/media",
        files={"file": ("photo.png", payload, "image/png")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["url"].startswith("/media/")
    assert body["kind"] == "image"

    # File exists on disk under the temporary media root.
    stored = list(tmp_path.iterdir())
    assert len(stored) == 1
    assert stored[0].suffix == ".png"


async def test_upload_non_image_rejected(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
) -> None:
    """A non-image content type is rejected with a domain error."""
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "storage_backend", "local")
    monkeypatch.setattr(cfg.settings, "media_root", str(tmp_path))

    category_id = await _make_category(client)
    product = await client.post(
        "/api/v1/admin/products",
        json={
            "category_id": category_id,
            "code": "SKU-TXT",
            "price": "1.00",
            "translations": _product_translations("txt", ("ru", "ro")),
        },
    )
    product_id = product.json()["id"]

    response = await client.post(
        f"/api/v1/admin/products/{product_id}/media",
        files={"file": ("note.txt", BytesIO(b"hello"), "text/plain")},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid"


# --------------------------------------------------------------------------- #
# Attributes
# --------------------------------------------------------------------------- #
async def test_attribute_crud_and_values(client: AsyncClient) -> None:
    """Attribute create/update + value create + link to a product."""
    created = await client.post(
        "/api/v1/admin/attributes",
        json={
            "code": "color",
            "translations": [
                {"lang": "ru", "name": "Цвет"},
                {"lang": "ro", "name": "Culoare"},
            ],
        },
    )
    assert created.status_code == 201, created.text
    attribute_id = created.json()["id"]
    assert {tr["lang"] for tr in created.json()["translations"]} == {"ru", "ro"}

    value = await client.post(
        f"/api/v1/admin/attributes/{attribute_id}/values",
        json={
            "translations": [
                {"lang": "ru", "value": "Красный"},
                {"lang": "ro", "value": "Roșu"},
            ]
        },
    )
    assert value.status_code == 201, value.text
    value_id = value.json()["id"]

    # Update the attribute code.
    updated = await client.patch(
        f"/api/v1/admin/attributes/{attribute_id}", json={"code": "colour"}
    )
    assert updated.status_code == 200
    assert updated.json()["code"] == "colour"

    # Link the value to a product.
    category_id = await _make_category(client)
    product = await client.post(
        "/api/v1/admin/products",
        json={
            "category_id": category_id,
            "code": "SKU-ATTR",
            "price": "7.00",
            "translations": _product_translations("attr", ("ru", "ro")),
        },
    )
    product_id = product.json()["id"]
    linked = await client.put(
        f"/api/v1/admin/products/{product_id}/attributes",
        json={"value_ids": [value_id]},
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["value_ids"] == [value_id]

    # Delete the value, then the attribute.
    assert (
        await client.delete(f"/api/v1/admin/attributes/values/{value_id}")
    ).status_code == 204
    assert (
        await client.delete(f"/api/v1/admin/attributes/{attribute_id}")
    ).status_code == 204


async def test_delete_category_with_children_conflict(client: AsyncClient) -> None:
    """Deleting a category that still has children is a conflict."""
    root = (
        await client.post("/api/v1/admin/categories", json=_cat_payload("R", "r"))
    ).json()
    await client.post(
        "/api/v1/admin/categories",
        json=_cat_payload("C", "c", parent_id=root["id"]),
    )
    response = await client.delete(f"/api/v1/admin/categories/{root['id']}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


# --------------------------------------------------------------------------- #
# Guard
# --------------------------------------------------------------------------- #
async def test_guard_blocks_non_staff(guest_client: AsyncClient) -> None:
    """Without an authenticated staff user the guard blocks the request."""
    response = await guest_client.post(
        "/api/v1/admin/categories", json=_cat_payload("X", "x")
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "http_error"


# --------------------------------------------------------------------------- #
# Slug validation (links must never degrade to a single letter/digit)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_slug", ["s", "5", "Bad Slug", "-lead", "double--hyphen"])
async def test_create_category_rejects_bad_slug(
    client: AsyncClient, bad_slug: str
) -> None:
    """A single char, spaces, or malformed hyphenation is rejected with 422."""
    payload = {
        "parent_id": None,
        "is_active": False,
        "translations": [
            {"lang": "ru", "name": "n-ru", "slug": bad_slug},
            {"lang": "ro", "name": "n-ro", "slug": "electronice-ro"},
        ],
    }
    response = await client.post("/api/v1/admin/categories", json=payload)
    assert response.status_code == 422, response.text


async def test_create_product_rejects_single_char_slug(client: AsyncClient) -> None:
    """A product translation slug of a single character is rejected with 422."""
    root = (
        await client.post("/api/v1/admin/categories", json=_cat_payload("Root", "root"))
    ).json()
    payload = {
        "category_id": root["id"],
        "code": "SKU-1",
        "price": "10.00",
        "translations": [
            {"lang": "ru", "name": "phone-ru", "slug": "x"},
            {"lang": "ro", "name": "phone-ro", "slug": "telefon-ro"},
        ],
    }
    response = await client.post("/api/v1/admin/products", json=payload)
    assert response.status_code == 422, response.text


async def test_slug_is_normalized_to_lowercase(client: AsyncClient) -> None:
    """A mixed-case slug is accepted and stored lowercased (clean links)."""
    payload = {
        "parent_id": None,
        "is_active": False,
        "translations": [
            {"lang": "ru", "name": "n-ru", "slug": "Tehnika-RU"},
            {"lang": "ro", "name": "n-ro", "slug": "Tehnica-RO"},
        ],
    }
    response = await client.post("/api/v1/admin/categories", json=payload)
    assert response.status_code == 201, response.text
    slugs = {t["lang"]: t["slug"] for t in response.json()["translations"]}
    assert slugs == {"ru": "tehnika-ru", "ro": "tehnica-ro"}
