"""Listing tests: cursor pagination over the product_card read-model."""

from decimal import Decimal

import pytest

from app.models.catalog import Product
from app.services.catalog_service import CatalogService, InvalidCursorError


async def _seed_products(seed, count: int) -> None:
    """Create ``count`` active products in category 2 with rebuilt cards."""
    session = seed["session"]
    service: CatalogService = seed["service"]
    for index in range(1, count + 1):
        await seed["add_product"](
            session,
            product_id=100 + index,
            category_id=2,
            code=f"P-{index:03d}",
            price=Decimal(f"{index * 10}.00"),
            qty=index,
            slugs={"ru": f"p-{index}-ru", "ro": f"p-{index}-ro"},
            names={"ru": f"Товар {index}", "ro": f"Produs {index}"},
        )
    await session.flush()
    await service.rebuild_cards([100 + i for i in range(1, count + 1)])


@pytest.mark.asyncio
async def test_listing_cursor_paginates(seed):
    """First page returns page_size items + a next_cursor; second page continues."""
    await seed["build_tree"]()
    await _seed_products(seed, 5)
    service: CatalogService = seed["service"]

    first = await service.list_products("telefoane", "ro", sort="newest", page_size=2)
    assert len(first.data) == 2
    assert first.next_cursor is not None
    # newest == highest product_id first
    assert first.data[0].product_id == 105
    assert first.data[1].product_id == 104

    second = await service.list_products(
        "telefoane", "ro", sort="newest", cursor=first.next_cursor, page_size=2
    )
    assert [c.product_id for c in second.data] == [103, 102]
    assert second.next_cursor is not None

    third = await service.list_products(
        "telefoane", "ro", sort="newest", cursor=second.next_cursor, page_size=2
    )
    assert [c.product_id for c in third.data] == [101]
    assert third.next_cursor is None


@pytest.mark.asyncio
async def test_listing_price_sort_asc(seed):
    """price_asc orders by ascending price and paginates by keyset."""
    await seed["build_tree"]()
    await _seed_products(seed, 4)
    service: CatalogService = seed["service"]

    page = await service.list_products("telefoane", "ro", sort="price_asc", page_size=2)
    assert [c.price for c in page.data] == [Decimal("10.00"), Decimal("20.00")]

    page2 = await service.list_products(
        "telefoane", "ro", sort="price_asc", cursor=page.next_cursor, page_size=2
    )
    assert [c.price for c in page2.data] == [Decimal("30.00"), Decimal("40.00")]


@pytest.mark.asyncio
async def test_listing_price_filter(seed):
    """price_min / price_max bound the listing."""
    await seed["build_tree"]()
    await _seed_products(seed, 5)
    service: CatalogService = seed["service"]

    page = await service.list_products(
        "telefoane",
        "ro",
        sort="price_asc",
        price_min=Decimal("20.00"),
        price_max=Decimal("40.00"),
        page_size=50,
    )
    assert [c.price for c in page.data] == [
        Decimal("20.00"),
        Decimal("30.00"),
        Decimal("40.00"),
    ]


@pytest.mark.asyncio
async def test_listing_bad_cursor_raises(seed):
    """A malformed cursor raises InvalidCursorError."""
    await seed["build_tree"]()
    await _seed_products(seed, 2)
    service: CatalogService = seed["service"]

    with pytest.raises(InvalidCursorError):
        await service.list_products(
            "telefoane", "ro", sort="newest", cursor="not-base64!!"
        )


@pytest.mark.asyncio
async def test_listing_cursor_sort_mismatch_raises(seed):
    """Reusing a cursor under a different sort is rejected."""
    await seed["build_tree"]()
    await _seed_products(seed, 4)
    service: CatalogService = seed["service"]

    first = await service.list_products("telefoane", "ro", sort="newest", page_size=2)
    with pytest.raises(InvalidCursorError):
        await service.list_products(
            "telefoane", "ro", sort="price_asc", cursor=first.next_cursor
        )


@pytest.mark.asyncio
async def test_listing_excludes_inactive(seed):
    """A single-lang inactive product never appears in the listing."""
    await seed["build_tree"]()
    session = seed["session"]
    service: CatalogService = seed["service"]

    await _seed_products(seed, 2)
    await seed["add_product"](
        session,
        product_id=200,
        category_id=2,
        code="P-HIDDEN",
        price=Decimal("5.00"),
        qty=0,
        slugs={"ro": "hidden-ro"},
        names={"ro": "Ascuns"},
        is_active=False,
    )
    await session.flush()
    await service.rebuild_card(200)

    page = await service.list_products("telefoane", "ro", page_size=50)
    assert 200 not in {c.product_id for c in page.data}


@pytest.mark.asyncio
async def test_listing_filters_by_attribute_values(seed):
    """value_ids facet filter: OR within an attribute, AND across attributes."""
    await seed["build_tree"]()
    session = seed["session"]
    service: CatalogService = seed["service"]
    m = seed["models"]

    for i in (1, 2, 3):
        await seed["add_product"](
            session,
            product_id=100 + i,
            category_id=2,
            code=f"AP-{i}",
            price=Decimal(f"{i * 10}.00"),
            qty=5,
            slugs={"ru": f"ap-{i}-ru", "ro": f"ap-{i}-ro"},
            names={"ru": f"Товар {i}", "ro": f"Produs {i}"},
        )
    # brand (10 Samsung / 11 Apple), color (20 Black / 21 White)
    session.add_all(
        [
            m["Attribute"](id=1, code="brand"),
            m["Attribute"](id=2, code="color"),
            m["AttributeValue"](id=10, attribute_id=1),
            m["AttributeValue"](id=11, attribute_id=1),
            m["AttributeValue"](id=20, attribute_id=2),
            m["AttributeValue"](id=21, attribute_id=2),
        ]
    )
    await session.flush()
    # p101 Samsung+Black, p102 Apple+Black, p103 Samsung+White
    session.add_all(
        [
            m["ProductAttribute"](product_id=101, value_id=10),
            m["ProductAttribute"](product_id=101, value_id=20),
            m["ProductAttribute"](product_id=102, value_id=11),
            m["ProductAttribute"](product_id=102, value_id=20),
            m["ProductAttribute"](product_id=103, value_id=10),
            m["ProductAttribute"](product_id=103, value_id=21),
        ]
    )
    await session.flush()
    await service.rebuild_cards([101, 102, 103])

    async def ids(value_ids):
        res = await service.list_products("telefoane", "ro", value_ids=value_ids)
        return {c.product_id for c in res.data}

    assert await ids([10]) == {101, 103}  # brand Samsung
    assert await ids([10, 11]) == {101, 102, 103}  # OR within brand
    assert await ids([10, 20]) == {101}  # Samsung AND Black
    assert await ids([11, 21]) == set()  # Apple AND White — no product


async def _seed_cross_category(seed) -> None:
    """Products across two sibling top-level categories, some on sale.

    Layout: cat 2 (telefoane, under root 1) holds p101 (sale) + p102 (no sale);
    a fresh sibling top-level cat 3 (accesorii) holds p103 (sale). A store-wide
    listing must span both subtrees; a category listing must not.
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
    await seed["add_product"](
        session,
        product_id=101,
        category_id=2,
        code="C2-1",
        price=Decimal("100.00"),
        old_price=Decimal("150.00"),
        qty=5,
        slugs={"ru": "c2-1-ru", "ro": "c2-1-ro"},
        names={"ru": "Товар 1", "ro": "Produs 1"},
    )
    await seed["add_product"](
        session,
        product_id=102,
        category_id=2,
        code="C2-2",
        price=Decimal("200.00"),
        qty=5,
        slugs={"ru": "c2-2-ru", "ro": "c2-2-ro"},
        names={"ru": "Товар 2", "ro": "Produs 2"},
    )
    await seed["add_product"](
        session,
        product_id=103,
        category_id=3,
        code="C3-1",
        price=Decimal("50.00"),
        old_price=Decimal("80.00"),
        qty=5,
        slugs={"ru": "c3-1-ru", "ro": "c3-1-ro"},
        names={"ru": "Товар 3", "ro": "Produs 3"},
    )
    await session.flush()
    await service.rebuild_cards([101, 102, 103])


@pytest.mark.asyncio
async def test_list_all_products_spans_categories(seed):
    """Store-wide listing returns products from disjoint category subtrees."""
    await seed["build_tree"]()
    await _seed_cross_category(seed)
    service: CatalogService = seed["service"]

    page = await service.list_all_products("ro", sort="newest", page_size=50)
    assert [c.product_id for c in page.data] == [103, 102, 101]

    # A single-category listing stays scoped: cat 3's product is excluded.
    scoped = await service.list_products("telefoane", "ro", page_size=50)
    assert {c.product_id for c in scoped.data} == {101, 102}


@pytest.mark.asyncio
async def test_list_all_products_on_sale_filter(seed):
    """on_sale keeps only cards with a struck-through old_price."""
    await seed["build_tree"]()
    await _seed_cross_category(seed)
    service: CatalogService = seed["service"]

    page = await service.list_all_products("ro", on_sale=True, page_size=50)
    assert {c.product_id for c in page.data} == {101, 103}
    for card in page.data:
        assert card.old_price is not None
        assert card.badge == "sale"


@pytest.mark.asyncio
async def test_list_all_products_cursor_paginates(seed):
    """Store-wide listing paginates by the same keyset cursor as categories."""
    await seed["build_tree"]()
    await _seed_cross_category(seed)
    service: CatalogService = seed["service"]

    first = await service.list_all_products("ro", sort="newest", page_size=2)
    assert [c.product_id for c in first.data] == [103, 102]
    assert first.next_cursor is not None

    second = await service.list_all_products(
        "ro", sort="newest", cursor=first.next_cursor, page_size=2
    )
    assert [c.product_id for c in second.data] == [101]
    assert second.next_cursor is None


@pytest.mark.asyncio
async def test_list_all_products_featured_filter(seed):
    """featured keeps only manually-curated (is_featured) cards."""
    await seed["build_tree"]()
    await _seed_products(seed, 3)  # products 101, 102, 103 in category 2
    session = seed["session"]
    service: CatalogService = seed["service"]

    product = await session.get(Product, 102)
    product.is_featured = True
    await session.flush()
    await service.rebuild_card(102)

    page = await service.list_all_products("ro", featured=True, page_size=50)
    assert {c.product_id for c in page.data} == {102}


@pytest.mark.asyncio
async def test_list_all_products_bad_cursor_raises(seed):
    """A malformed store-wide cursor raises InvalidCursorError."""
    await seed["build_tree"]()
    await _seed_cross_category(seed)
    service: CatalogService = seed["service"]

    with pytest.raises(InvalidCursorError):
        await service.list_all_products("ro", cursor="not-base64!!")


@pytest.mark.asyncio
async def test_list_all_products_api(seed, client):
    """GET /catalog/products routes (not shadowed by /products/{slug}) + filters."""
    await seed["build_tree"]()
    await _seed_cross_category(seed)

    resp = await client.get(
        "/api/v1/catalog/products", params={"lang": "ro", "on_sale": "true"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert {p["product_id"] for p in body["data"]} == {101, 103}
