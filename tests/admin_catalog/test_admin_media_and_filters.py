"""API tests for the §6.1 catalog additions: media delete/reorder + product
back-office filters (is_active / low_stock / on_sale).

Media tests run against the local storage backend (files land under a temporary
``media_root``) so deletion can assert the file is gone, not just the row.
"""

from io import BytesIO

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


def _translations(slug: str) -> list[dict]:
    """Both-language product translations for the given slug."""
    return [
        {"lang": "ru", "name": f"{slug}-ru", "slug": f"{slug}-ru"},
        {"lang": "ro", "name": f"{slug}-ro", "slug": f"{slug}-ro"},
    ]


async def _make_category(client: AsyncClient) -> int:
    """Create a category and return its id."""
    payload = {
        "parent_id": None,
        "is_active": False,
        "translations": [
            {"lang": "ru", "name": "Cat-ru", "slug": "cat-ru"},
            {"lang": "ro", "name": "Cat-ro", "slug": "cat-ro"},
        ],
    }
    return (await client.post("/api/v1/admin/categories", json=payload)).json()["id"]


async def _make_product(
    client: AsyncClient,
    category_id: int,
    code: str,
    *,
    price: str = "10.00",
    old_price: str | None = None,
    qty: int = 100,
    is_active: bool = False,
) -> int:
    """Create a product and return its id."""
    body: dict = {
        "category_id": category_id,
        "code": code,
        "price": price,
        "qty": qty,
        "is_active": is_active,
        "translations": _translations(code.lower()),
    }
    if old_price is not None:
        body["old_price"] = old_price
    resp = await client.post("/api/v1/admin/products", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _upload(client: AsyncClient, product_id: int, name: str) -> dict:
    """Upload one PNG to a product and return the created media row."""
    resp = await client.post(
        f"/api/v1/admin/products/{product_id}/media",
        files={"file": (name, BytesIO(b"\x89PNG\r\n\x1a\nx"), "image/png")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _use_local_storage(monkeypatch, tmp_path) -> None:
    """Point the storage backend at a temporary local media root."""
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "storage_backend", "local")
    monkeypatch.setattr(cfg.settings, "media_root", str(tmp_path))
    monkeypatch.setattr(cfg.settings, "media_url_prefix", "/media")


# --------------------------------------------------------------------------- #
# Media delete
# --------------------------------------------------------------------------- #
async def test_delete_media_removes_row_and_file(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
) -> None:
    """Deleting media drops the row and best-effort unlinks the stored file."""
    _use_local_storage(monkeypatch, tmp_path)
    category_id = await _make_category(client)
    product_id = await _make_product(client, category_id, "SKU-DEL")
    media = await _upload(client, product_id, "a.png")
    assert len(list(tmp_path.iterdir())) == 1

    resp = await client.delete(
        f"/api/v1/admin/products/{product_id}/media/{media['id']}"
    )
    assert resp.status_code == 204, resp.text
    assert list(tmp_path.iterdir()) == []


async def test_delete_media_wrong_product_is_404(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
) -> None:
    """A media id that belongs to another product is not deletable here."""
    _use_local_storage(monkeypatch, tmp_path)
    category_id = await _make_category(client)
    owner = await _make_product(client, category_id, "SKU-OWN")
    other = await _make_product(client, category_id, "SKU-OTH")
    media = await _upload(client, owner, "b.png")

    resp = await client.delete(f"/api/v1/admin/products/{other}/media/{media['id']}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- #
# Media reorder
# --------------------------------------------------------------------------- #
async def test_reorder_media_sets_positions(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
) -> None:
    """Reorder writes positions from the given order; first id = position 0."""
    _use_local_storage(monkeypatch, tmp_path)
    category_id = await _make_category(client)
    product_id = await _make_product(client, category_id, "SKU-ORD")
    first = await _upload(client, product_id, "1.png")
    second = await _upload(client, product_id, "2.png")
    third = await _upload(client, product_id, "3.png")

    new_order = [third["id"], first["id"], second["id"]]
    resp = await client.put(
        f"/api/v1/admin/products/{product_id}/media/reorder",
        json={"ordered_ids": new_order},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    assert [row["id"] for row in rows] == new_order
    assert [row["position"] for row in rows] == [0, 1, 2]


async def test_reorder_media_rejects_non_permutation(
    client: AsyncClient,
    tmp_path,
    monkeypatch,
) -> None:
    """A partial / mismatched id set is rejected (stale client guard)."""
    _use_local_storage(monkeypatch, tmp_path)
    category_id = await _make_category(client)
    product_id = await _make_product(client, category_id, "SKU-BAD")
    first = await _upload(client, product_id, "1.png")
    await _upload(client, product_id, "2.png")

    resp = await client.put(
        f"/api/v1/admin/products/{product_id}/media/reorder",
        json={"ordered_ids": [first["id"]]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid"


# --------------------------------------------------------------------------- #
# Product filters
# --------------------------------------------------------------------------- #
async def test_filter_on_sale(client: AsyncClient) -> None:
    """``on_sale=true`` returns only products with old_price > price."""
    category_id = await _make_category(client)
    sale = await _make_product(
        client, category_id, "SALE-1", price="8.00", old_price="10.00"
    )
    # old_price present but not greater than price → not on sale.
    await _make_product(
        client, category_id, "NOSALE-1", price="10.00", old_price="10.00"
    )
    await _make_product(client, category_id, "PLAIN-1", price="5.00")

    resp = await client.get("/api/v1/admin/products", params={"on_sale": True})
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["data"]}
    assert ids == {sale}


async def test_filter_low_stock(client: AsyncClient) -> None:
    """``low_stock=true`` returns only products at/below the threshold (5)."""
    category_id = await _make_category(client)
    low = await _make_product(client, category_id, "LOW-1", qty=2)
    zero = await _make_product(client, category_id, "LOW-0", qty=0)
    await _make_product(client, category_id, "HIGH-1", qty=50)

    resp = await client.get("/api/v1/admin/products", params={"low_stock": True})
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["data"]}
    assert ids == {low, zero}


async def test_get_single_product(client: AsyncClient) -> None:
    """GET /admin/products/{id} returns the full admin product view."""
    category_id = await _make_category(client)
    product_id = await _make_product(client, category_id, "SKU-GET")
    resp = await client.get(f"/api/v1/admin/products/{product_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == product_id
    assert body["code"] == "SKU-GET"
    assert {tr["lang"] for tr in body["translations"]} == {"ru", "ro"}


async def test_get_single_product_404(client: AsyncClient) -> None:
    """An unknown product id yields 404."""
    resp = await client.get("/api/v1/admin/products/999999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_list_categories_includes_inactive(client: AsyncClient) -> None:
    """GET /admin/categories returns all categories, active or not, flat."""
    category_id = await _make_category(client)
    resp = await client.get("/api/v1/admin/categories")
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert category_id in ids
    # The created category is inactive by default — must still be listed.
    row = next(r for r in resp.json() if r["id"] == category_id)
    assert row["is_active"] is False
    assert {tr["lang"] for tr in row["translations"]} == {"ru", "ro"}


async def test_limit_counts_distinct_products_not_join_rows(
    client: AsyncClient,
) -> None:
    """``limit`` bounds distinct products, not the (ru+ro) joined rows.

    Regression for the P1 where the outer-join to ProductTranslation produced two
    rows per product, so ``.limit(limit)`` capped the joined rows and dedup then
    yielded only ceil(limit/2) products (limit=4 → 2). Each product below has both
    a ``ru`` and a ``ro`` translation.
    """
    category_id = await _make_category(client)
    for index in range(6):
        await _make_product(client, category_id, f"LIMIT-{index}")

    resp = await client.get("/api/v1/admin/products", params={"limit": 4})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 4
    # One row per product (no duplicated ids from the two-language join).
    assert len({item["id"] for item in data}) == 4


async def test_search_by_code_and_by_name(client: AsyncClient) -> None:
    """Search still matches ``code`` and a translation ``name`` in either lang."""
    category_id = await _make_category(client)
    target = await _make_product(client, category_id, "FIND-ME")
    await _make_product(client, category_id, "OTHER-1")

    # By product code.
    resp = await client.get("/api/v1/admin/products", params={"search": "FIND-ME"})
    assert resp.status_code == 200, resp.text
    assert {item["id"] for item in resp.json()["data"]} == {target}

    # By translation name — helpers create names "<code-lower>-ru" / "-ro".
    resp = await client.get("/api/v1/admin/products", params={"search": "find-me-ro"})
    assert resp.status_code == 200, resp.text
    assert {item["id"] for item in resp.json()["data"]} == {target}


async def test_filter_is_active(client: AsyncClient) -> None:
    """``is_active=false`` returns only inactive products."""
    category_id = await _make_category(client)
    inactive = await _make_product(client, category_id, "INACT-1", is_active=False)
    active = await _make_product(client, category_id, "ACT-1", is_active=True)

    resp = await client.get("/api/v1/admin/products", params={"is_active": False})
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["data"]}
    assert inactive in ids
    assert active not in ids
