"""Related-products tests: in-stock cross-sell siblings from the same category."""

from decimal import Decimal

import pytest

from app.services.catalog_service import CatalogService


async def _seed_siblings(seed) -> None:
    """Seed category 2 with the 'current' product + in-stock and OOS siblings.

    Layout (all in category 2 / telefoane):
      * p101 — current product (in stock)
      * p102, p103, p104 — in-stock siblings (newer ids == newer products)
      * p105 — out-of-stock sibling (qty=0), must be excluded from cross-sell
    A sibling in *another* category (p201 in category 3) must never appear.
    """
    session = seed["session"]
    service: CatalogService = seed["service"]

    await seed["add_category"](
        session,
        category_id=3,
        parent_id=None,
        path=[3],
        depth=0,
        position=1,
        slugs={"ru": "aksessuary", "ro": "accesorii"},
        names={"ru": "Аксессуары", "ro": "Accesorii"},
    )
    for index in range(1, 5):
        await seed["add_product"](
            session,
            product_id=100 + index,
            category_id=2,
            code=f"R-{index:03d}",
            price=Decimal(f"{index * 10}.00"),
            qty=index,  # all in stock (qty >= 1)
            slugs={"ru": f"r-{index}-ru", "ro": f"r-{index}-ro"},
            names={"ru": f"Товар {index}", "ro": f"Produs {index}"},
        )
    # Out-of-stock sibling in the same category (qty=0) — excluded from cross-sell.
    await seed["add_product"](
        session,
        product_id=105,
        category_id=2,
        code="R-OOS",
        price=Decimal("99.00"),
        qty=0,
        slugs={"ru": "r-oos-ru", "ro": "r-oos-ro"},
        names={"ru": "Нет в наличии", "ro": "Fără stoc"},
    )
    # A product in a different category — must never appear as related.
    await seed["add_product"](
        session,
        product_id=201,
        category_id=3,
        code="R-OTHER",
        price=Decimal("15.00"),
        qty=5,
        slugs={"ru": "r-other-ru", "ro": "r-other-ro"},
        names={"ru": "Другая", "ro": "Alta"},
    )
    await session.flush()
    await service.rebuild_cards([101, 102, 103, 104, 105, 201])


@pytest.mark.asyncio
async def test_related_returns_instock_siblings_excludes_self(seed):
    """Siblings of the current product, self + OOS + other-category excluded."""
    await seed["build_tree"]()
    await _seed_siblings(seed)
    service: CatalogService = seed["service"]

    cards = await service.related_products("r-1-ro", "ro", limit=8)

    ids = [c.product_id for c in cards]
    # newest-first, current (101) excluded, OOS (105) excluded, other cat (201) excluded
    assert ids == [104, 103, 102]


@pytest.mark.asyncio
async def test_related_respects_limit(seed):
    """The limit caps the number of related cards."""
    await seed["build_tree"]()
    await _seed_siblings(seed)
    service: CatalogService = seed["service"]

    cards = await service.related_products("r-1-ro", "ro", limit=2)

    assert [c.product_id for c in cards] == [104, 103]


@pytest.mark.asyncio
async def test_related_card_fields_localized(seed):
    """Cards carry listing fields: name in the requested lang, slug, price, in_stock."""
    await seed["build_tree"]()
    await _seed_siblings(seed)
    service: CatalogService = seed["service"]

    cards = await service.related_products("r-1-ro", "ro", limit=8)

    top = cards[0]  # product 104
    assert top.product_id == 104
    assert top.name == "Produs 4"
    assert top.slug == "r-4-ro"
    assert top.price == Decimal("40.00")
    assert top.in_stock is True

    # requesting ru yields the ru name/slug for the same product
    ru_cards = await service.related_products("r-1-ru", "ru", limit=8)
    ru_top = ru_cards[0]
    assert ru_top.product_id == 104
    assert ru_top.name == "Товар 4"
    assert ru_top.slug == "r-4-ru"


@pytest.mark.asyncio
async def test_related_unknown_slug_returns_empty(seed):
    """A nonexistent slug yields an empty list (best-effort, never 404s)."""
    await seed["build_tree"]()
    await _seed_siblings(seed)
    service: CatalogService = seed["service"]

    cards = await service.related_products("does-not-exist", "ro", limit=8)

    assert cards == []


@pytest.mark.asyncio
async def test_related_no_siblings_returns_empty(seed):
    """A product alone in its category yields an empty list."""
    await seed["build_tree"]()
    session = seed["session"]
    service: CatalogService = seed["service"]

    await seed["add_product"](
        session,
        product_id=500,
        category_id=2,
        code="LONELY",
        price=Decimal("10.00"),
        qty=3,
        slugs={"ru": "lonely-ru", "ro": "lonely-ro"},
        names={"ru": "Одинокий", "ro": "Singur"},
    )
    await session.flush()
    await service.rebuild_card(500)

    cards = await service.related_products("lonely-ro", "ro", limit=8)

    assert cards == []


@pytest.mark.asyncio
async def test_related_api_returns_cards(seed, client):
    """GET /catalog/products/{slug}/related returns the sibling cards."""
    await seed["build_tree"]()
    await _seed_siblings(seed)

    resp = await client.get(
        "/api/v1/catalog/products/r-1-ro/related", params={"lang": "ro", "limit": 8}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert [c["product_id"] for c in body] == [104, 103, 102]
    assert body[0]["name"] == "Produs 4"
    assert body[0]["in_stock"] is True


@pytest.mark.asyncio
async def test_related_api_unknown_slug_returns_empty(seed, client):
    """Unknown slug returns 200 with an empty list, not 404."""
    await seed["build_tree"]()
    await _seed_siblings(seed)

    resp = await client.get(
        "/api/v1/catalog/products/ghost/related", params={"lang": "ro"}
    )

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_related_api_limit_out_of_range_rejected(seed, client):
    """limit outside 1..24 is rejected by query validation (422)."""
    await seed["build_tree"]()
    await _seed_siblings(seed)

    resp = await client.get(
        "/api/v1/catalog/products/r-1-ro/related", params={"lang": "ro", "limit": 0}
    )

    assert resp.status_code == 422
