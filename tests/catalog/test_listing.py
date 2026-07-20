"""Listing tests: cursor pagination over the product_card read-model."""

from decimal import Decimal

import pytest

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
