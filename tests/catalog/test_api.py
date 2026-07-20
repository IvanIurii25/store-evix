"""API-level tests for the catalog router (mounted under /api/v1)."""

from decimal import Decimal

import pytest

from app.services.catalog_service import CatalogService


async def _seed_pdp(seed) -> None:
    """Create one full product with media + one attribute + rebuilt cards."""
    session = seed["session"]
    service: CatalogService = seed["service"]
    models = seed["models"]

    await seed["add_product"](
        session,
        product_id=300,
        category_id=2,
        code="PDP-1",
        price=Decimal("999.00"),
        old_price=Decimal("1099.00"),
        qty=4,
        slugs={"ru": "pdp-ru", "ro": "pdp-ro"},
        names={"ru": "ПДП Товар", "ro": "PDP Produs"},
    )
    await session.flush()
    session.add(
        models["Media"](id=10, product_id=300, url="http://img/a.jpg", position=0)
    )
    session.add(
        models["Media"](id=11, product_id=300, url="http://img/b.jpg", position=1)
    )

    session.add(models["Attribute"](id=1, code="ram"))
    session.add(
        models["AttributeTranslation"](attribute_id=1, lang="ru", name="Память")
    )
    session.add(
        models["AttributeTranslation"](attribute_id=1, lang="ro", name="Memorie")
    )
    session.add(models["AttributeValue"](id=1, attribute_id=1))
    session.add(
        models["AttributeValueTranslation"](value_id=1, lang="ru", value="8 ГБ")
    )
    session.add(
        models["AttributeValueTranslation"](value_id=1, lang="ro", value="8 GB")
    )
    await session.flush()
    session.add(models["ProductAttribute"](product_id=300, value_id=1))
    await session.flush()
    await service.rebuild_card(300)


@pytest.mark.asyncio
async def test_api_categories_tree(seed, client):
    """GET /catalog/categories returns the tree."""
    await seed["build_tree"]()

    resp = await client.get("/api/v1/catalog/categories?lang=ro")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["slug"] == "electronice"
    assert body[0]["children"][0]["slug"] == "telefoane"


@pytest.mark.asyncio
async def test_api_category_detail(seed, client):
    """GET /catalog/categories/{slug} returns detail with breadcrumbs."""
    await seed["build_tree"]()

    resp = await client.get("/api/v1/catalog/categories/telefoane?lang=ro")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 2
    assert [c["id"] for c in body["breadcrumbs"]] == [1, 2]


@pytest.mark.asyncio
async def test_api_category_detail_404(seed, client):
    """Unknown category slug returns a unified 404 envelope."""
    await seed["build_tree"]()

    resp = await client.get("/api/v1/catalog/categories/missing?lang=ro")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "http_error"


@pytest.mark.asyncio
async def test_api_pdp_both_slugs(seed, client):
    """GET /catalog/products/{slug} returns PDP with attributes, media, both slugs."""
    await seed["build_tree"]()
    await _seed_pdp(seed)

    resp = await client.get("/api/v1/catalog/products/pdp-ro?lang=ro")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 300
    assert body["in_stock"] is True
    assert body["slug"] == "pdp-ro"
    # both language slugs present for the switcher
    slug_map = {entry["lang"]: entry["slug"] for entry in body["slugs"]}
    assert slug_map == {"ru": "pdp-ru", "ro": "pdp-ro"}
    # localized attribute
    assert body["attributes"][0]["code"] == "ram"
    assert body["attributes"][0]["name"] == "Memorie"
    assert body["attributes"][0]["values"] == ["8 GB"]
    # media ordered by position
    assert [m["url"] for m in body["media"]] == [
        "http://img/a.jpg",
        "http://img/b.jpg",
    ]
    # breadcrumbs from path
    assert [c["id"] for c in body["breadcrumbs"]] == [1, 2]


@pytest.mark.asyncio
async def test_api_pdp_404(seed, client):
    """Unknown product slug returns 404."""
    await seed["build_tree"]()

    resp = await client.get("/api/v1/catalog/products/ghost?lang=ro")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_listing_with_cursor(seed, client):
    """GET .../products paginates via next_cursor."""
    await seed["build_tree"]()
    session = seed["session"]
    service: CatalogService = seed["service"]
    for index in range(1, 4):
        await seed["add_product"](
            session,
            product_id=400 + index,
            category_id=2,
            code=f"L-{index}",
            price=Decimal(f"{index}.00"),
            qty=1,
            slugs={"ru": f"l-{index}-ru", "ro": f"l-{index}-ro"},
            names={"ru": f"Л {index}", "ro": f"L {index}"},
        )
    await session.flush()
    await service.rebuild_cards([401, 402, 403])

    resp = await client.get(
        "/api/v1/catalog/categories/telefoane/products?lang=ro&sort=newest"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body and "next_cursor" in body
    assert body["data"][0]["product_id"] == 403


@pytest.mark.asyncio
async def test_api_bad_lang_400(seed, client):
    """An unsupported ?lang= is rejected with 400."""
    await seed["build_tree"]()

    resp = await client.get("/api/v1/catalog/categories?lang=fr")

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "http_error"


@pytest.mark.asyncio
async def test_api_lang_default_ro(seed, client):
    """Without ?lang= and no header, the default ro is used."""
    await seed["build_tree"]()

    resp = await client.get("/api/v1/catalog/categories")

    assert resp.status_code == 200
    assert resp.json()[0]["slug"] == "electronice"
