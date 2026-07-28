"""API + behaviour tests for search + facets (stage B3).

Covers the B3 DoD:

* Russian stemming: querying «телефоны» finds a product named «телефон».
* Article-code (``product.code``) matches outside the FTS.
* ``ts_rank`` relevance ordering (the more relevant product ranks higher).
* Facets return attribute values + counts + price bounds for the subtree.
* Inactive products never appear in facet counts / price bounds.

Data is inserted via ``product_translation`` so the DB trigger builds
``search_vector`` inside the test transaction (real FTS path).
"""

from decimal import Decimal

import pytest

from app.services.catalog_service import CatalogService


@pytest.mark.asyncio
async def test_russian_stemming_matches_inflected_form(seed, client):
    """«телефоны» (plural) finds a product whose name is «Телефон» (singular)."""
    await seed["build_tree"]()
    session = seed["session"]
    service: CatalogService = seed["service"]

    await seed["add_product"](
        session,
        product_id=100,
        category_id=2,
        code="PHONE-1",
        price=Decimal("4999.00"),
        qty=3,
        slugs={"ru": "telefon-ru", "ro": "telefon-ro"},
        names={"ru": "Телефон Samsung", "ro": "Telefon Samsung"},
    )
    await session.flush()
    await service.rebuild_card(100)

    resp = await client.get("/api/v1/search?q=телефоны&lang=ru")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["data"][0]["card"]["product_id"] == 100
    assert body["data"][0]["card"]["name"] == "Телефон Samsung"


@pytest.mark.asyncio
async def test_code_match_outside_fts(seed, client):
    """A query equal to the article code matches even when name/desc do not."""
    await seed["build_tree"]()
    session = seed["session"]
    service: CatalogService = seed["service"]

    await seed["add_product"](
        session,
        product_id=110,
        category_id=2,
        code="XYZ-777",
        price=Decimal("1500.00"),
        qty=1,
        slugs={"ru": "nabor-ru", "ro": "nabor-ro"},
        names={"ru": "Набор посуды", "ro": "Set de vase"},
    )
    await session.flush()
    await service.rebuild_card(110)

    resp = await client.get("/api/v1/search?q=xyz-777&lang=ru")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["data"][0]["card"]["product_id"] == 110


@pytest.mark.asyncio
async def test_ts_rank_orders_more_relevant_first(seed, client):
    """The product mentioning the term in both name and description ranks higher."""
    await seed["build_tree"]()
    session = seed["session"]
    service: CatalogService = seed["service"]

    # Relevant: term in name (weight A) and description (weight B).
    await seed["add_product"](
        session,
        product_id=120,
        category_id=2,
        code="LAPTOP-A",
        price=Decimal("20000.00"),
        qty=2,
        slugs={"ru": "laptop-a-ru", "ro": "laptop-a-ro"},
        names={"ru": "Ноутбук игровой", "ro": "Laptop de gaming"},
        descriptions={"ru": "Мощный ноутбук для игр", "ro": "Laptop puternic"},
    )
    # Less relevant: term only once in description.
    await seed["add_product"](
        session,
        product_id=121,
        category_id=2,
        code="BAG-A",
        price=Decimal("500.00"),
        qty=5,
        slugs={"ru": "bag-a-ru", "ro": "bag-a-ro"},
        names={"ru": "Сумка", "ro": "Geanta"},
        descriptions={"ru": "Сумка для ноутбук", "ro": "Geanta pentru laptop"},
    )
    await session.flush()
    await service.rebuild_cards([120, 121])

    resp = await client.get("/api/v1/search?q=ноутбук&lang=ru")

    assert resp.status_code == 200
    body = resp.json()
    ids = [hit["card"]["product_id"] for hit in body["data"]]
    assert ids[0] == 120  # more relevant first
    assert set(ids) == {120, 121}
    assert body["data"][0]["rank"] >= body["data"][1]["rank"]


@pytest.mark.asyncio
async def test_search_empty_query_rejected(seed, client):
    """A blank query is rejected by request validation (min_length=1)."""
    await seed["build_tree"]()

    resp = await client.get("/api/v1/search?q=&lang=ru")

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


@pytest.mark.asyncio
async def test_search_no_results(seed, client):
    """A query with no matches returns an empty, well-formed page."""
    await seed["build_tree"]()
    session = seed["session"]
    service: CatalogService = seed["service"]
    await seed["add_product"](
        session,
        product_id=130,
        category_id=2,
        code="TV-1",
        price=Decimal("8000.00"),
        qty=1,
        slugs={"ru": "tv-ru", "ro": "tv-ro"},
        names={"ru": "Телевизор", "ro": "Televizor"},
    )
    await session.flush()
    await service.rebuild_card(130)

    resp = await client.get("/api/v1/search?q=xxxxxxxx&lang=ru")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["data"] == []


async def _seed_facet_products(seed) -> None:
    """Two active phones (RAM 8/16) + one inactive, with brand+ram attributes."""
    session = seed["session"]
    service: CatalogService = seed["service"]
    models = seed["models"]

    # Attribute dictionary: ram + brand, each with two localized values.
    session.add(models["Attribute"](id=1, code="ram"))
    session.add(
        models["AttributeTranslation"](attribute_id=1, lang="ru", name="Память")
    )
    session.add(
        models["AttributeTranslation"](attribute_id=1, lang="ro", name="Memorie")
    )
    session.add(models["Attribute"](id=2, code="brand"))
    session.add(models["AttributeTranslation"](attribute_id=2, lang="ru", name="Бренд"))
    session.add(models["AttributeTranslation"](attribute_id=2, lang="ro", name="Brand"))

    session.add(models["AttributeValue"](id=1, attribute_id=1))  # ram 8
    session.add(
        models["AttributeValueTranslation"](value_id=1, lang="ru", value="8 ГБ")
    )
    session.add(
        models["AttributeValueTranslation"](value_id=1, lang="ro", value="8 GB")
    )
    session.add(models["AttributeValue"](id=2, attribute_id=1))  # ram 16
    session.add(
        models["AttributeValueTranslation"](value_id=2, lang="ru", value="16 ГБ")
    )
    session.add(
        models["AttributeValueTranslation"](value_id=2, lang="ro", value="16 GB")
    )
    session.add(models["AttributeValue"](id=3, attribute_id=2))  # brand Samsung
    session.add(
        models["AttributeValueTranslation"](value_id=3, lang="ru", value="Samsung")
    )
    session.add(
        models["AttributeValueTranslation"](value_id=3, lang="ro", value="Samsung")
    )
    await session.flush()

    # Active product A: ram 8 + Samsung, price 4999.
    await seed["add_product"](
        session,
        product_id=200,
        category_id=2,
        code="F-A",
        price=Decimal("4999.00"),
        qty=2,
        slugs={"ru": "fa-ru", "ro": "fa-ro"},
        names={"ru": "Товар А", "ro": "Produs A"},
    )
    # Active product B: ram 8 + Samsung, price 9999.
    await seed["add_product"](
        session,
        product_id=201,
        category_id=2,
        code="F-B",
        price=Decimal("9999.00"),
        qty=1,
        slugs={"ru": "fb-ru", "ro": "fb-ro"},
        names={"ru": "Товар Б", "ro": "Produs B"},
    )
    # Inactive product C: ram 16, price 99999 — must NOT appear in facets.
    await seed["add_product"](
        session,
        product_id=202,
        category_id=2,
        code="F-C",
        price=Decimal("99999.00"),
        qty=0,
        slugs={"ru": "fc-ru", "ro": "fc-ro"},
        names={"ru": "Товар В", "ro": "Produs C"},
        is_active=False,
    )
    await session.flush()

    session.add(models["ProductAttribute"](product_id=200, value_id=1))  # ram 8
    session.add(models["ProductAttribute"](product_id=200, value_id=3))  # Samsung
    session.add(models["ProductAttribute"](product_id=201, value_id=1))  # ram 8
    session.add(models["ProductAttribute"](product_id=201, value_id=3))  # Samsung
    session.add(models["ProductAttribute"](product_id=202, value_id=2))  # ram 16
    await session.flush()

    # rebuild_card on the inactive product produces no card rows (hidden).
    await service.rebuild_cards([200, 201, 202])


@pytest.mark.asyncio
async def test_facets_values_counts_and_price_bounds(seed, client):
    """Facets return values + counts + price bounds for the active subtree."""
    await seed["build_tree"]()
    await _seed_facet_products(seed)

    resp = await client.get("/api/v1/catalog/categories/electronice/facets?lang=ro")

    assert resp.status_code == 200
    body = resp.json()

    # Only active products (A, B) contribute: ram 8 -> 2, Samsung -> 2.
    by_code = {attr["code"]: attr for attr in body["attributes"]}
    assert set(by_code) == {"ram", "brand"}

    ram = by_code["ram"]
    assert ram["name"] == "Memorie"
    ram_counts = {v["value"]: v["count"] for v in ram["values"]}
    assert ram_counts == {"8 GB": 2}  # 16 GB belongs only to inactive C -> absent

    brand_counts = {v["value"]: v["count"] for v in by_code["brand"]["values"]}
    assert brand_counts == {"Samsung": 2}

    # Price bounds ignore the inactive 99999 product.
    assert body["price_min"] == "4999.00"
    assert body["price_max"] == "9999.00"


@pytest.mark.asyncio
async def test_facets_subtree_from_root(seed, client):
    """Facets on the root category cover products in a child (subtree via path)."""
    await seed["build_tree"]()
    await _seed_facet_products(seed)

    # Products live under child (id=2); querying the root slug must still see them.
    resp = await client.get("/api/v1/catalog/categories/electronice/facets?lang=ro")

    assert resp.status_code == 200
    body = resp.json()
    total_ram = sum(
        v["count"]
        for attr in body["attributes"]
        if attr["code"] == "ram"
        for v in attr["values"]
    )
    assert total_ram == 2


@pytest.mark.asyncio
async def test_facets_unknown_category_404(seed, client):
    """Unknown category slug yields a unified 404 envelope."""
    await seed["build_tree"]()

    resp = await client.get("/api/v1/catalog/categories/missing/facets?lang=ro")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
