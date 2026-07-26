"""P1 read-path tests for variable products: PDP variant view + card aggregates."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.catalog import (
    Attribute,
    AttributeTranslation,
    AttributeValue,
    AttributeValueTranslation,
    Media,
    Product,
    ProductAttribute,
    ProductCard,
    ProductVariant,
    ProductVariantValue,
    ProductVariationAttribute,
)
from app.services.catalog_service import CatalogService, PublicationError


async def _seed_color_attribute(session) -> dict[str, int]:
    """Create a ``color`` attribute with two localized values; return value ids."""
    attr = Attribute(code="color")
    session.add(attr)
    await session.flush()
    session.add_all(
        [
            AttributeTranslation(attribute_id=attr.id, lang="ru", name="Цвет"),
            AttributeTranslation(attribute_id=attr.id, lang="ro", name="Culoare"),
        ]
    )
    ids: dict[str, int] = {"attribute_id": attr.id}
    for key, ru, ro in (("beige", "Бежевый", "Bej"), ("grey", "Серый", "Gri")):
        value = AttributeValue(attribute_id=attr.id)
        session.add(value)
        await session.flush()
        session.add_all(
            [
                AttributeValueTranslation(value_id=value.id, lang="ru", value=ru),
                AttributeValueTranslation(value_id=value.id, lang="ro", value=ro),
            ]
        )
        ids[key] = value.id
    return ids


async def _seed_variable_product(
    seed, *, product_id: int, slug: str, prices: tuple[str, str], qtys: tuple[int, int]
) -> dict[str, int]:
    """Two-variant variable product (color beige/grey) with a per-variant image."""
    session = seed["session"]
    await seed["build_tree"]()
    await seed["add_product"](
        session,
        product_id=product_id,
        category_id=2,
        code=f"VAR-{product_id}",
        price=Decimal(prices[0]),
        qty=0,
        slugs={"ru": f"{slug}-ru", "ro": f"{slug}-ro"},
        names={"ru": "Коврик", "ro": "Covor"},
    )
    await session.flush()
    product = await session.get(Product, product_id)
    product.has_variants = True

    ids = await _seed_color_attribute(session)
    session.add(
        ProductVariationAttribute(
            product_id=product_id, attribute_id=ids["attribute_id"], position=0
        )
    )

    made: dict[str, int] = {}
    for idx, (key, price, qty) in enumerate(
        (("beige", prices[0], qtys[0]), ("grey", prices[1], qtys[1]))
    ):
        variant = ProductVariant(
            product_id=product_id,
            code=f"SKU-{product_id}-{key}",
            price=Decimal(price),
            qty=qty,
            position=idx,
            is_active=True,
        )
        session.add(variant)
        await session.flush()
        session.add(ProductVariantValue(variant_id=variant.id, value_id=ids[key]))
        made[key] = variant.id
    # A per-variant image only on the beige variant.
    session.add(
        Media(
            product_id=product_id,
            variant_id=made["beige"],
            url="https://media.test/beige.webp",
            position=0,
        )
    )
    await session.flush()
    return {**ids, **{f"variant_{k}": v for k, v in made.items()}}


@pytest.mark.asyncio
async def test_pdp_variant_view_price_range(seed):
    """A variable product's PDP exposes selectors, variants and a from–to range."""
    ids = await _seed_variable_product(
        seed, product_id=500, slug="kovrik", prices=("179.00", "249.00"), qtys=(5, 0)
    )
    detail = await seed["service"].get_product("kovrik-ro", "ro")

    assert detail.has_variants is True
    assert detail.price_min == Decimal("179.00")
    assert detail.price == Decimal("179.00")  # "from" price
    assert detail.price_max == Decimal("249.00")
    assert detail.in_stock is True  # beige qty>0

    assert len(detail.variation_attributes) == 1
    selector = detail.variation_attributes[0]
    assert selector.name == "Culoare"
    assert {v.value for v in selector.values} == {"Bej", "Gri"}

    assert len(detail.variants) == 2
    beige = next(v for v in detail.variants if v.id == ids["variant_beige"])
    assert beige.price == Decimal("179.00")
    assert beige.in_stock is True
    assert beige.value_ids == [ids["beige"]]
    assert beige.image_url == "https://media.test/beige.webp"
    grey = next(v for v in detail.variants if v.id == ids["variant_grey"])
    assert grey.in_stock is False  # qty 0
    assert grey.image_url is None


@pytest.mark.asyncio
async def test_variable_card_aggregates_min_max_and_stock(seed):
    """rebuild_card writes min price, price_max range top, and any-variant stock."""
    await _seed_variable_product(
        seed, product_id=501, slug="kovrik2", prices=("179.00", "249.00"), qtys=(0, 3)
    )
    await seed["service"].rebuild_card(501)

    cards = (
        (
            await seed["session"].execute(
                select(ProductCard).where(ProductCard.product_id == 501)
            )
        )
        .scalars()
        .all()
    )
    assert len(cards) == 2  # ru + ro
    for card in cards:
        assert card.price == Decimal("179.00")
        assert card.price_max == Decimal("249.00")
        assert card.in_stock is True  # grey qty>0


@pytest.mark.asyncio
async def test_uniform_price_variants_have_no_range(seed):
    """When every variant shares a price, price_max is None (no range shown)."""
    await _seed_variable_product(
        seed, product_id=502, slug="kovrik3", prices=("199.00", "199.00"), qtys=(2, 2)
    )
    detail = await seed["service"].get_product("kovrik3-ro", "ro")
    assert detail.price_min == Decimal("199.00")
    assert detail.price_max is None

    await seed["service"].rebuild_card(502)
    card = (
        await seed["session"].execute(
            select(ProductCard).where(ProductCard.product_id == 502).limit(1)
        )
    ).scalar_one()
    assert card.price == Decimal("199.00")
    assert card.price_max is None


@pytest.mark.asyncio
async def test_publication_rule_variable_requires_active_variant(seed):
    """A variable product cannot be published without an active variant (P2)."""
    session = seed["session"]
    await seed["build_tree"]()
    await seed["add_product"](
        session,
        product_id=510,
        category_id=2,
        code="VP-510",
        price=Decimal("100.00"),
        qty=0,
        slugs={"ru": "novar-ru", "ro": "novar-ro"},
        names={"ru": "Товар", "ro": "Produs"},
    )
    await session.flush()
    product = await session.get(Product, 510)
    product.has_variants = True
    await session.flush()
    service: CatalogService = seed["service"]

    with pytest.raises(PublicationError):
        await service.assert_publishable(510)

    session.add(
        ProductVariant(
            product_id=510,
            price=Decimal("100.00"),
            qty=3,
            position=0,
            is_active=True,
        )
    )
    await session.flush()
    await service.assert_publishable(510)  # no raise now


@pytest.mark.asyncio
async def test_variation_attributes_excluded_from_characteristics(seed):
    """A selector attribute must not be duplicated in the flat characteristics."""
    ids = await _seed_variable_product(
        seed, product_id=520, slug="kovrik-dedup", prices=("179.00", "249.00"),
        qtys=(5, 5),
    )
    session = seed["session"]
    # Mimic import/backfill: the color values are also linked as flat attributes.
    session.add_all(
        [
            ProductAttribute(product_id=520, value_id=ids["beige"]),
            ProductAttribute(product_id=520, value_id=ids["grey"]),
        ]
    )
    await session.flush()

    detail = await seed["service"].get_product("kovrik-dedup-ro", "ro")
    # "color" is a selector → present in variation_attributes, absent from the
    # flat characteristics list (no duplication on the PDP).
    assert any(va.code == "color" for va in detail.variation_attributes)
    assert all(a.code != "color" for a in detail.attributes)
