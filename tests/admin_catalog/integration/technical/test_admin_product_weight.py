"""Shipping weight on products and variants (Nova Post phase P0).

``weight_g`` is what a carrier prices a parcel by. It is deliberately nullable —
the imported catalogue has no weights, and "unknown" must stay distinguishable
from "really light" so the delivery service can substitute a configured default
instead of quoting zero.

Covered: round-trip through create / read / patch, the partial-update rule (a
PATCH that doesn't mention weight must not erase it), the ``no_weight=true``
back-office queue, per-variant override, and the input bounds that stop a
grams-for-kilograms typo from reaching a carrier.
"""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

_PRODUCTS = "/api/v1/admin/products"


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
            {"lang": "ru", "name": "Вес-ru", "slug": "weight-cat-ru"},
            {"lang": "ro", "name": "Вес-ro", "slug": "weight-cat-ro"},
        ],
    }
    return (await client.post("/api/v1/admin/categories", json=payload)).json()["id"]


async def _make_product(
    client: AsyncClient,
    category_id: int,
    code: str,
    *,
    weight_g: int | None = None,
) -> dict:
    """Create a product (optionally with a weight) and return the response body."""
    body: dict = {
        "category_id": category_id,
        "code": code,
        "price": "10.00",
        "qty": 100,
        "translations": _translations(code.lower()),
    }
    if weight_g is not None:
        body["weight_g"] = weight_g
    resp = await client.post(_PRODUCTS, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_weight_defaults_to_null(client: AsyncClient) -> None:
    """A product created without a weight reports ``None``, not 0."""
    category_id = await _make_category(client)

    body = await _make_product(client, category_id, "W-NULL")

    assert body["weight_g"] is None


async def test_weight_round_trips_through_create_and_read(client: AsyncClient) -> None:
    """A weight given at creation is persisted and read back."""
    category_id = await _make_category(client)
    created = await _make_product(client, category_id, "W-SET", weight_g=1250)

    resp = await client.get(f"{_PRODUCTS}/{created['id']}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["weight_g"] == 1250


async def test_patch_sets_weight(client: AsyncClient) -> None:
    """PATCH assigns a weight to a product that had none."""
    category_id = await _make_category(client)
    created = await _make_product(client, category_id, "W-PATCH")

    resp = await client.patch(f"{_PRODUCTS}/{created['id']}", json={"weight_g": 800})

    assert resp.status_code == 200, resp.text
    assert resp.json()["weight_g"] == 800


async def test_patch_without_weight_keeps_it(client: AsyncClient) -> None:
    """An unrelated PATCH must not clear an entered weight (partial update)."""
    category_id = await _make_category(client)
    created = await _make_product(client, category_id, "W-KEEP", weight_g=640)

    resp = await client.patch(f"{_PRODUCTS}/{created['id']}", json={"qty": 7})

    assert resp.status_code == 200, resp.text
    assert resp.json()["weight_g"] == 640


async def test_patch_can_clear_weight_explicitly(client: AsyncClient) -> None:
    """Sending ``null`` explicitly resets the weight back to "not entered"."""
    category_id = await _make_category(client)
    created = await _make_product(client, category_id, "W-CLEAR", weight_g=900)

    resp = await client.patch(f"{_PRODUCTS}/{created['id']}", json={"weight_g": None})

    assert resp.status_code == 200, resp.text
    assert resp.json()["weight_g"] is None


async def test_filter_no_weight(client: AsyncClient) -> None:
    """``no_weight=true`` returns exactly the products still missing a weight."""
    category_id = await _make_category(client)
    missing_a = await _make_product(client, category_id, "W-MISS-A")
    missing_b = await _make_product(client, category_id, "W-MISS-B")
    await _make_product(client, category_id, "W-HAS", weight_g=300)

    resp = await client.get(_PRODUCTS, params={"no_weight": True})

    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["data"]}
    assert ids == {missing_a["id"], missing_b["id"]}


async def test_list_exposes_weight(client: AsyncClient) -> None:
    """The back-office list carries ``weight_g`` so the UI can flag what's missing."""
    category_id = await _make_category(client)
    created = await _make_product(client, category_id, "W-LIST", weight_g=450)

    resp = await client.get(_PRODUCTS, params={"search": "W-LIST"})

    assert resp.status_code == 200, resp.text
    rows = {item["id"]: item for item in resp.json()["data"]}
    assert rows[created["id"]]["weight_g"] == 450


@pytest.mark.parametrize("bad", [-1, 200_001])
async def test_weight_out_of_bounds_is_422(client: AsyncClient, bad: int) -> None:
    """Negative or absurd weights are rejected before they reach a carrier."""
    category_id = await _make_category(client)

    resp = await client.post(
        _PRODUCTS,
        json={
            "category_id": category_id,
            "code": f"W-BAD-{bad}",
            "price": "10.00",
            "qty": 1,
            "weight_g": bad,
            "translations": _translations(f"w-bad-{abs(bad)}"),
        },
    )

    assert resp.status_code == 422, resp.text


async def test_variant_weight_overrides_product(client: AsyncClient) -> None:
    """A variant carries its own weight; the product-level one stays independent."""
    category_id = await _make_category(client)
    product = await _make_product(client, category_id, "W-VAR", weight_g=1000)
    product_id = product["id"]

    attr = await client.post(
        "/api/v1/admin/attributes",
        json={
            "code": "size-w",
            "translations": [
                {"lang": "ru", "name": "Размер"},
                {"lang": "ro", "name": "Mărime"},
            ],
        },
    )
    assert attr.status_code == 201, attr.text
    attribute_id = attr.json()["id"]
    value = await client.post(
        f"/api/v1/admin/attributes/{attribute_id}/values",
        json={
            "translations": [
                {"lang": "ru", "value": "L"},
                {"lang": "ro", "value": "L"},
            ]
        },
    )
    assert value.status_code == 201, value.text
    value_id = value.json()["id"]

    set_attrs = await client.put(
        f"{_PRODUCTS}/{product_id}/variation-attributes",
        json={"attribute_ids": [attribute_id]},
    )
    assert set_attrs.status_code == 200, set_attrs.text

    created = await client.post(
        f"{_PRODUCTS}/{product_id}/variants",
        json={
            "value_ids": [value_id],
            "price": "12.00",
            "qty": 5,
            "weight_g": 2500,
        },
    )

    assert created.status_code == 201, created.text
    assert created.json()["weight_g"] == 2500
    # The product keeps its own weight — the variant value is an override, not a
    # write-through.
    product_after = await client.get(f"{_PRODUCTS}/{product_id}")
    assert product_after.json()["weight_g"] == 1000


async def test_variant_weight_patch(client: AsyncClient) -> None:
    """A variant's weight can be corrected after creation."""
    category_id = await _make_category(client)
    product = await _make_product(client, category_id, "W-VAR2")
    product_id = product["id"]

    attr = await client.post(
        "/api/v1/admin/attributes",
        json={
            "code": "color-w",
            "translations": [
                {"lang": "ru", "name": "Цвет"},
                {"lang": "ro", "name": "Culoare"},
            ],
        },
    )
    attribute_id = attr.json()["id"]
    value = await client.post(
        f"/api/v1/admin/attributes/{attribute_id}/values",
        json={
            "translations": [
                {"lang": "ru", "value": "Синий"},
                {"lang": "ro", "value": "Albastru"},
            ]
        },
    )
    value_id = value.json()["id"]
    await client.put(
        f"{_PRODUCTS}/{product_id}/variation-attributes",
        json={"attribute_ids": [attribute_id]},
    )
    variant = await client.post(
        f"{_PRODUCTS}/{product_id}/variants",
        json={"value_ids": [value_id], "price": "12.00", "qty": 5},
    )
    assert variant.status_code == 201, variant.text
    assert variant.json()["weight_g"] is None

    resp = await client.patch(
        f"/api/v1/admin/variants/{variant.json()['id']}",
        json={"weight_g": 175},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["weight_g"] == 175
