"""API tests for the admin variant CRUD (P2e, variable products).

Covers the seven back-office endpoints added for variable products:

* ``GET  /products/{id}/variants`` — list;
* ``PUT  /products/{id}/variation-attributes`` — set selectors + ``has_variants``
  toggle (true when non-empty, false when emptied);
* ``POST /products/{id}/variants`` — create one full combination (+ duplicate,
  incomplete and non-selector value validations);
* ``POST /products/{id}/variants/generate`` — bulk cartesian fill (skips existing);
* ``PATCH /variants/{id}`` — partial mutable-field update;
* ``DELETE /variants/{id}`` — delete + null bound media;
* ``PUT  /products/{id}/media/{media_id}/variant`` — bind/unbind ownership checks.

Fixtures (``client``, ``guest_client``, ``db_session``) come from the package
conftest. Run with ``EVIX_TEST_DB=evix_test_admin_catalog``.
"""

import itertools
from io import BytesIO

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Media, Product, ProductVariant, ProductVariantValue

pytestmark = pytest.mark.asyncio

# Monotonic counter for globally-unique category slugs (translation slug is unique
# per lang across the whole table; category creates commit, so within one test the
# outer SAVEPOINT does not shield sibling rows from the constraint).
_slug_seq = itertools.count(1)


# --------------------------------------------------------------------------- #
# Builders (all via the public admin API, mirroring the existing suite)
# --------------------------------------------------------------------------- #
def _png_bytes(width: int = 40, height: int = 40) -> bytes:
    """Return an in-memory, genuinely decodable PNG (the upload gate needs one)."""
    buffer = BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def _cat_payload(name: str, slug: str) -> dict:
    """Build a two-language category create payload."""
    return {
        "parent_id": None,
        "is_active": False,
        "translations": [
            {"lang": "ru", "name": f"{name}-ru", "slug": f"{slug}-ru"},
            {"lang": "ro", "name": f"{name}-ro", "slug": f"{slug}-ro"},
        ],
    }


async def _make_category(client: AsyncClient) -> int:
    """Create a category with a unique slug and return its id."""
    suffix = next(_slug_seq)
    resp = await client.post(
        "/api/v1/admin/categories", json=_cat_payload(f"Cat{suffix}", f"cat-{suffix}")
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _make_product(client: AsyncClient, code: str, price: str = "10.00") -> int:
    """Create a two-language product and return its id."""
    category_id = await _make_category(client)
    resp = await client.post(
        "/api/v1/admin/products",
        json={
            "category_id": category_id,
            "code": code,
            "price": price,
            "qty": 0,
            "is_active": False,
            "translations": [
                {"lang": "ru", "name": f"{code}-ru", "slug": f"{code.lower()}-ru"},
                {"lang": "ro", "name": f"{code}-ro", "slug": f"{code.lower()}-ro"},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _make_attribute(client: AsyncClient, code: str) -> int:
    """Create an attribute and return its id."""
    resp = await client.post(
        "/api/v1/admin/attributes",
        json={
            "code": code,
            "translations": [
                {"lang": "ru", "name": f"{code}-ru"},
                {"lang": "ro", "name": f"{code}-ro"},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _make_value(client: AsyncClient, attribute_id: int, label: str) -> int:
    """Create an attribute value and return its id."""
    resp = await client.post(
        f"/api/v1/admin/attributes/{attribute_id}/values",
        json={
            "translations": [
                {"lang": "ru", "value": f"{label}-ru"},
                {"lang": "ro", "value": f"{label}-ro"},
            ]
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _set_variation_attrs(
    client: AsyncClient, product_id: int, attribute_ids: list[int]
) -> dict:
    """Set a product's ordered variation-selector attributes; return ProductOut."""
    resp = await client.put(
        f"/api/v1/admin/products/{product_id}/variation-attributes",
        json={"attribute_ids": attribute_ids},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _assign_values(
    client: AsyncClient, product_id: int, value_ids: list[int]
) -> None:
    """Link attribute values to a product (source for generate)."""
    resp = await client.put(
        f"/api/v1/admin/products/{product_id}/attributes",
        json={"value_ids": value_ids},
    )
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# Endpoint 2 — variation attributes + has_variants toggle
# --------------------------------------------------------------------------- #
async def test_set_variation_attributes_toggles_has_variants(
    client: AsyncClient,
) -> None:
    """A non-empty list turns has_variants on (ordered); emptying turns it off."""
    product_id = await _make_product(client, "VAR-TOGGLE")
    color = await _make_attribute(client, "color")
    size = await _make_attribute(client, "size")

    body = await _set_variation_attrs(client, product_id, [size, color])
    assert body["has_variants"] is True
    # Order is preserved (position = index).
    assert body["variation_attribute_ids"] == [size, color]

    emptied = await _set_variation_attrs(client, product_id, [])
    assert emptied["has_variants"] is False
    assert emptied["variation_attribute_ids"] == []


async def test_set_variation_attributes_unknown_attribute_404(
    client: AsyncClient,
) -> None:
    """A non-existent attribute id is rejected as not found."""
    product_id = await _make_product(client, "VAR-BADATTR")
    resp = await client.put(
        f"/api/v1/admin/products/{product_id}/variation-attributes",
        json={"attribute_ids": [999_999]},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "not_found"


async def test_cannot_clear_variation_attributes_with_variants(
    client: AsyncClient,
) -> None:
    """Emptying the selectors while variants still exist is rejected (guard)."""
    product_id = await _make_product(client, "VAR-CLEARGUARD")
    color = await _make_attribute(client, "color")
    red = await _make_value(client, color, "red")
    await _set_variation_attrs(client, product_id, [color])
    created = await client.post(
        f"/api/v1/admin/products/{product_id}/variants",
        json={"value_ids": [red], "price": "10.00"},
    )
    assert created.status_code == 201, created.text

    resp = await client.put(
        f"/api/v1/admin/products/{product_id}/variation-attributes",
        json={"attribute_ids": []},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid"


async def test_set_variation_attributes_active_no_variants_409(
    client: AsyncClient,
) -> None:
    """Marking an ACTIVE product variable while it has no variants yet is a clean
    409 (publication rule), not an unhandled 500."""
    product_id = await _make_product(client, "VAR-PUBGUARD")
    activate = await client.patch(
        f"/api/v1/admin/products/{product_id}", json={"is_active": True}
    )
    assert activate.status_code == 200, activate.text
    color = await _make_attribute(client, "pubcolor")

    resp = await client.put(
        f"/api/v1/admin/products/{product_id}/variation-attributes",
        json={"attribute_ids": [color]},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "not_publishable"


# --------------------------------------------------------------------------- #
# Endpoint 3 — create variant + validations
# --------------------------------------------------------------------------- #
async def test_create_variant_happy_path(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A complete combination creates a variant with its value links."""
    product_id = await _make_product(client, "VAR-CREATE")
    color = await _make_attribute(client, "color")
    size = await _make_attribute(client, "size")
    red = await _make_value(client, color, "red")
    large = await _make_value(client, size, "large")
    await _set_variation_attrs(client, product_id, [color, size])

    resp = await client.post(
        f"/api/v1/admin/products/{product_id}/variants",
        json={
            "value_ids": [red, large],
            "price": "12.50",
            "qty": 3,
            "code": "SKU-RL",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["product_id"] == product_id
    assert sorted(body["value_ids"]) == sorted([red, large])
    assert body["qty"] == 3

    links = (
        (
            await db_session.execute(
                select(ProductVariantValue.value_id).where(
                    ProductVariantValue.variant_id == body["id"]
                )
            )
        )
        .scalars()
        .all()
    )
    assert sorted(links) == sorted([red, large])


async def test_create_variant_duplicate_combo_409(client: AsyncClient) -> None:
    """A second variant with the same value-set is a conflict."""
    product_id = await _make_product(client, "VAR-DUP")
    color = await _make_attribute(client, "color")
    red = await _make_value(client, color, "red")
    await _set_variation_attrs(client, product_id, [color])

    first = await client.post(
        f"/api/v1/admin/products/{product_id}/variants",
        json={"value_ids": [red], "price": "10.00"},
    )
    assert first.status_code == 201, first.text

    dup = await client.post(
        f"/api/v1/admin/products/{product_id}/variants",
        json={"value_ids": [red], "price": "11.00"},
    )
    assert dup.status_code == 409, dup.text
    assert dup.json()["error"]["code"] == "conflict"


async def test_create_variant_incomplete_combo_rejected(client: AsyncClient) -> None:
    """Fewer values than variation attributes is rejected (incomplete combo)."""
    product_id = await _make_product(client, "VAR-INCOMPLETE")
    color = await _make_attribute(client, "color")
    size = await _make_attribute(client, "size")
    red = await _make_value(client, color, "red")
    await _make_value(client, size, "large")
    await _set_variation_attrs(client, product_id, [color, size])

    resp = await client.post(
        f"/api/v1/admin/products/{product_id}/variants",
        json={"value_ids": [red], "price": "10.00"},  # missing a size value
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid"


async def test_create_variant_value_not_a_selector_rejected(
    client: AsyncClient,
) -> None:
    """A value whose attribute is not a variation selector is rejected."""
    product_id = await _make_product(client, "VAR-NONSEL")
    color = await _make_attribute(client, "color")
    material = await _make_attribute(client, "material")
    red = await _make_value(client, color, "red")
    cotton = await _make_value(client, material, "cotton")
    # Only ``color`` is a selector; ``material`` is not.
    await _set_variation_attrs(client, product_id, [color])

    resp = await client.post(
        f"/api/v1/admin/products/{product_id}/variants",
        json={"value_ids": [red, cotton], "price": "10.00"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid"


async def test_create_variant_without_selectors_rejected(client: AsyncClient) -> None:
    """Creating a variant before any selector is set is rejected."""
    product_id = await _make_product(client, "VAR-NOSEL")
    color = await _make_attribute(client, "color")
    red = await _make_value(client, color, "red")

    resp = await client.post(
        f"/api/v1/admin/products/{product_id}/variants",
        json={"value_ids": [red], "price": "10.00"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid"


# --------------------------------------------------------------------------- #
# Endpoint 1 — list
# --------------------------------------------------------------------------- #
async def test_list_variants(client: AsyncClient) -> None:
    """The list endpoint returns every variant of the product."""
    product_id = await _make_product(client, "VAR-LIST")
    color = await _make_attribute(client, "color")
    red = await _make_value(client, color, "red")
    blue = await _make_value(client, color, "blue")
    await _set_variation_attrs(client, product_id, [color])
    for value_id in (red, blue):
        resp = await client.post(
            f"/api/v1/admin/products/{product_id}/variants",
            json={"value_ids": [value_id], "price": "10.00"},
        )
        assert resp.status_code == 201, resp.text

    listed = await client.get(f"/api/v1/admin/products/{product_id}/variants")
    assert listed.status_code == 200, listed.text
    data = listed.json()["data"]
    assert len(data) == 2
    all_values = {v for row in data for v in row["value_ids"]}
    assert all_values == {red, blue}


# --------------------------------------------------------------------------- #
# Endpoint 4 — generate (cartesian, skip existing)
# --------------------------------------------------------------------------- #
async def test_generate_creates_and_skips_existing(client: AsyncClient) -> None:
    """Generate fills the missing cartesian combos and skips existing ones."""
    product_id = await _make_product(client, "VAR-GEN")
    color = await _make_attribute(client, "color")
    size = await _make_attribute(client, "size")
    red = await _make_value(client, color, "red")
    blue = await _make_value(client, color, "blue")
    small = await _make_value(client, size, "small")
    large = await _make_value(client, size, "large")
    await _set_variation_attrs(client, product_id, [color, size])
    # The universe of values must be assigned to the product first.
    await _assign_values(client, product_id, [red, blue, small, large])

    # Pre-create one of the four combinations (red + small).
    pre = await client.post(
        f"/api/v1/admin/products/{product_id}/variants",
        json={"value_ids": [red, small], "price": "10.00"},
    )
    assert pre.status_code == 201, pre.text

    generated = await client.post(
        f"/api/v1/admin/products/{product_id}/variants/generate"
    )
    assert generated.status_code == 201, generated.text
    body = generated.json()
    # 2x2 = 4 combos; one existed → 3 created, 1 skipped.
    assert len(body["created"]) == 3
    assert body["skipped"] == 1

    # Re-running is idempotent: nothing new, all four skipped.
    again = await client.post(f"/api/v1/admin/products/{product_id}/variants/generate")
    assert again.status_code == 201, again.text
    assert again.json()["created"] == []
    assert again.json()["skipped"] == 4


async def test_generate_without_selectors_rejected(client: AsyncClient) -> None:
    """Generate requires variation attributes to be set first."""
    product_id = await _make_product(client, "VAR-GEN-NOSEL")
    resp = await client.post(f"/api/v1/admin/products/{product_id}/variants/generate")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid"


# --------------------------------------------------------------------------- #
# Endpoint 5 — patch mutable fields
# --------------------------------------------------------------------------- #
async def test_patch_variant_updates_mutable_fields(client: AsyncClient) -> None:
    """A partial patch updates price/qty/active and leaves the combo intact."""
    product_id = await _make_product(client, "VAR-PATCH")
    color = await _make_attribute(client, "color")
    red = await _make_value(client, color, "red")
    await _set_variation_attrs(client, product_id, [color])
    created = await client.post(
        f"/api/v1/admin/products/{product_id}/variants",
        json={"value_ids": [red], "price": "10.00", "qty": 1},
    )
    variant_id = created.json()["id"]

    patched = await client.patch(
        f"/api/v1/admin/variants/{variant_id}",
        json={"price": "20.00", "qty": 9, "is_active": False},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["price"] == "20.00"
    assert body["qty"] == 9
    assert body["is_active"] is False
    assert body["value_ids"] == [red]  # identity unchanged


async def test_patch_variant_empty_body_rejected(client: AsyncClient) -> None:
    """An empty patch (no mutable field) is a 422 validation error."""
    product_id = await _make_product(client, "VAR-PATCH-EMPTY")
    color = await _make_attribute(client, "color")
    red = await _make_value(client, color, "red")
    await _set_variation_attrs(client, product_id, [color])
    created = await client.post(
        f"/api/v1/admin/products/{product_id}/variants",
        json={"value_ids": [red], "price": "10.00"},
    )
    variant_id = created.json()["id"]

    resp = await client.patch(f"/api/v1/admin/variants/{variant_id}", json={})
    assert resp.status_code == 422, resp.text


async def test_patch_missing_variant_404(client: AsyncClient) -> None:
    """Patching a non-existent variant is not found."""
    resp = await client.patch("/api/v1/admin/variants/999999", json={"qty": 1})
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- #
# Endpoint 6 — delete nulls bound media
# --------------------------------------------------------------------------- #
async def test_delete_variant_nulls_bound_media(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path,
    monkeypatch,
) -> None:
    """Deleting a variant removes its rows and returns its media to the gallery."""
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "storage_backend", "local")
    monkeypatch.setattr(cfg.settings, "media_root", str(tmp_path))
    monkeypatch.setattr(cfg.settings, "media_url_prefix", "/media")

    product_id = await _make_product(client, "VAR-DEL")
    color = await _make_attribute(client, "color")
    red = await _make_value(client, color, "red")
    await _set_variation_attrs(client, product_id, [color])
    created = await client.post(
        f"/api/v1/admin/products/{product_id}/variants",
        json={"value_ids": [red], "price": "10.00"},
    )
    variant_id = created.json()["id"]

    media = await client.post(
        f"/api/v1/admin/products/{product_id}/media",
        files={"file": ("v.png", BytesIO(_png_bytes()), "image/png")},
    )
    assert media.status_code == 201, media.text
    media_id = media.json()["id"]
    bound = await client.put(
        f"/api/v1/admin/products/{product_id}/media/{media_id}/variant",
        json={"variant_id": variant_id},
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["variant_id"] == variant_id

    deleted = await client.delete(f"/api/v1/admin/variants/{variant_id}")
    assert deleted.status_code == 204, deleted.text

    db_session.expire_all()
    variant_row = await db_session.get(ProductVariant, variant_id)
    assert variant_row is None
    media_row = await db_session.get(Media, media_id)
    assert media_row is not None
    assert media_row.variant_id is None  # returned to the shared gallery


# --------------------------------------------------------------------------- #
# Endpoint 7 — media bind/unbind ownership checks
# --------------------------------------------------------------------------- #
async def test_bind_and_unbind_media(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
) -> None:
    """A product's media binds to its own variant and unbinds back to None."""
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "storage_backend", "local")
    monkeypatch.setattr(cfg.settings, "media_root", str(tmp_path))
    monkeypatch.setattr(cfg.settings, "media_url_prefix", "/media")

    product_id = await _make_product(client, "VAR-BIND")
    color = await _make_attribute(client, "color")
    red = await _make_value(client, color, "red")
    await _set_variation_attrs(client, product_id, [color])
    created = await client.post(
        f"/api/v1/admin/products/{product_id}/variants",
        json={"value_ids": [red], "price": "10.00"},
    )
    variant_id = created.json()["id"]
    media = await client.post(
        f"/api/v1/admin/products/{product_id}/media",
        files={"file": ("v.png", BytesIO(_png_bytes()), "image/png")},
    )
    media_id = media.json()["id"]

    bound = await client.put(
        f"/api/v1/admin/products/{product_id}/media/{media_id}/variant",
        json={"variant_id": variant_id},
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["variant_id"] == variant_id

    unbound = await client.put(
        f"/api/v1/admin/products/{product_id}/media/{media_id}/variant",
        json={"variant_id": None},
    )
    assert unbound.status_code == 200, unbound.text
    assert unbound.json()["variant_id"] is None


async def test_bind_media_wrong_product_variant_404(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
) -> None:
    """Binding media to a variant of another product is rejected."""
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "storage_backend", "local")
    monkeypatch.setattr(cfg.settings, "media_root", str(tmp_path))
    monkeypatch.setattr(cfg.settings, "media_url_prefix", "/media")

    # Product A with a media row.
    product_a = await _make_product(client, "VAR-BIND-A")
    media = await client.post(
        f"/api/v1/admin/products/{product_a}/media",
        files={"file": ("a.png", BytesIO(_png_bytes()), "image/png")},
    )
    media_id = media.json()["id"]

    # Product B with a variant.
    product_b = await _make_product(client, "VAR-BIND-B")
    color = await _make_attribute(client, "color")
    red = await _make_value(client, color, "red")
    await _set_variation_attrs(client, product_b, [color])
    created = await client.post(
        f"/api/v1/admin/products/{product_b}/variants",
        json={"value_ids": [red], "price": "10.00"},
    )
    foreign_variant = created.json()["id"]

    # Try to bind A's media to B's variant, addressed under product A.
    resp = await client.put(
        f"/api/v1/admin/products/{product_a}/media/{media_id}/variant",
        json={"variant_id": foreign_variant},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- #
# Guard
# --------------------------------------------------------------------------- #
async def test_variants_guard_blocks_non_staff(guest_client: AsyncClient) -> None:
    """The variant endpoints are gated by current_staff (401 without a token)."""
    resp = await guest_client.get("/api/v1/admin/products/1/variants")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "http_error"


# --------------------------------------------------------------------------- #
# has_variants stays correct across create (endpoint 3 flips it on)
# --------------------------------------------------------------------------- #
async def test_create_variant_keeps_has_variants_true(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Creating a variant leaves the product flagged variable."""
    product_id = await _make_product(client, "VAR-FLAG")
    color = await _make_attribute(client, "color")
    red = await _make_value(client, color, "red")
    await _set_variation_attrs(client, product_id, [color])
    await client.post(
        f"/api/v1/admin/products/{product_id}/variants",
        json={"value_ids": [red], "price": "10.00"},
    )

    db_session.expire_all()
    product = await db_session.get(Product, product_id)
    assert product is not None
    assert product.has_variants is True
