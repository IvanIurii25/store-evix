"""Bulk seed: a large catalog for performance measurement (§5.5).

Generates a category tree plus a configurable number of active products (default
2000), each with ru+ro translations, 2-3 attribute values, 1-2 media rows, and a
rebuilt ``product_card`` read-model. Intended to be pointed at a dedicated
benchmark database (``evix_bench``) so :mod:`scripts.bench` can measure hot paths
against realistic volume.

Idempotent: if the catalog already holds at least the requested number of active
products, it exits without writing.

Usage::

    DATABASE_URL=postgresql+asyncpg://evix:evix@localhost:55432/evix_bench \\
        uv run python scripts/seed_bulk.py            # 2000 products
    ... uv run python scripts/seed_bulk.py --count 5000
    EVIX_SEED_COUNT=5000 ... uv run python scripts/seed_bulk.py

The target database comes from ``DATABASE_URL`` (via
:data:`app.core.config.settings`) — the same engine the app uses — so the volume
lands wherever the benchmark run will read from.
"""

import argparse
import asyncio
import os
import time
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_factory
from app.models.catalog import (
    Attribute,
    AttributeTranslation,
    AttributeValue,
    AttributeValueTranslation,
    Category,
    CategoryTranslation,
    Media,
    Product,
    ProductAttribute,
    ProductTranslation,
)
from app.services.catalog_service import CatalogService

# Default catalog volume; overridable via ``--count`` or ``EVIX_SEED_COUNT``.
DEFAULT_COUNT: int = 2000
# Number of leaf categories the products are spread across.
LEAF_CATEGORY_COUNT: int = 10
# Flush/commit granularity so a big run never holds one giant transaction.
BATCH_SIZE: int = 500

# Attribute value pools (both languages) — each product gets one brand + one color
# + one size, giving 3 facetable attributes per product.
_BRANDS: tuple[str, ...] = ("Samsung", "Apple", "Xiaomi", "Sony", "Huawei")
_COLORS: dict[str, dict[str, str]] = {
    "Black": {"ru": "Чёрный", "ro": "Negru"},
    "White": {"ru": "Белый", "ro": "Alb"},
    "Silver": {"ru": "Серебристый", "ro": "Argintiu"},
    "Blue": {"ru": "Синий", "ro": "Albastru"},
}
_SIZES: tuple[str, ...] = ("64GB", "128GB", "256GB")


def _parse_count() -> int:
    """Resolve the requested product count from ``--count`` / env / default.

    Returns:
        int: The number of products to ensure exist.
    """
    parser = argparse.ArgumentParser(
        description="Bulk-seed the catalog for benchmarks."
    )
    parser.add_argument(
        "--count",
        type=int,
        default=int(os.environ.get("EVIX_SEED_COUNT", DEFAULT_COUNT)),
        help=f"Number of products to generate (default {DEFAULT_COUNT}).",
    )
    args = parser.parse_args()
    return max(1, args.count)


async def _build_category_tree(session: AsyncSession) -> list[int]:
    """Create one root + ``LEAF_CATEGORY_COUNT`` leaf categories (ru+ro).

    Args:
        session: Active async session.

    Returns:
        list[int]: The leaf category ids to attach products to.
    """
    root = Category(path=[], depth=0, is_active=True, position=0)
    session.add(root)
    await session.flush()
    root.path = [root.id]
    session.add_all(
        [
            CategoryTranslation(
                category_id=root.id, lang="ru", name="Каталог", slug="katalog"
            ),
            CategoryTranslation(
                category_id=root.id, lang="ro", name="Catalog", slug="catalog"
            ),
        ]
    )

    leaf_ids: list[int] = []
    for index in range(LEAF_CATEGORY_COUNT):
        leaf = Category(
            path=[], depth=1, is_active=True, position=index, parent_id=root.id
        )
        session.add(leaf)
        await session.flush()
        leaf.path = [root.id, leaf.id]
        session.add_all(
            [
                CategoryTranslation(
                    category_id=leaf.id,
                    lang="ru",
                    name=f"Категория {index + 1}",
                    slug=f"kategoriya-{index + 1}",
                ),
                CategoryTranslation(
                    category_id=leaf.id,
                    lang="ro",
                    name=f"Categoria {index + 1}",
                    slug=f"categoria-{index + 1}",
                ),
            ]
        )
        leaf_ids.append(leaf.id)
    await session.flush()
    return leaf_ids


async def _build_attributes(
    session: AsyncSession,
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Create the brand/color/size attributes and their values (ru+ro).

    Args:
        session: Active async session.

    Returns:
        tuple: Three maps ``label -> attribute_value_id`` for brand, color, size.
    """
    brand = Attribute(code="brand")
    color = Attribute(code="color")
    size = Attribute(code="size")
    session.add_all([brand, color, size])
    await session.flush()
    session.add_all(
        [
            AttributeTranslation(attribute_id=brand.id, lang="ru", name="Бренд"),
            AttributeTranslation(attribute_id=brand.id, lang="ro", name="Brand"),
            AttributeTranslation(attribute_id=color.id, lang="ru", name="Цвет"),
            AttributeTranslation(attribute_id=color.id, lang="ro", name="Culoare"),
            AttributeTranslation(attribute_id=size.id, lang="ru", name="Память"),
            AttributeTranslation(attribute_id=size.id, lang="ro", name="Memorie"),
        ]
    )

    brand_ids = await _make_values(
        session, brand.id, {b: {"ru": b, "ro": b} for b in _BRANDS}
    )
    color_ids = await _make_values(session, color.id, _COLORS)
    size_ids = await _make_values(
        session, size.id, {s: {"ru": s, "ro": s} for s in _SIZES}
    )
    await session.flush()
    return brand_ids, color_ids, size_ids


async def _make_values(
    session: AsyncSession,
    attribute_id: int,
    labels: dict[str, dict[str, str]],
) -> dict[str, int]:
    """Insert attribute values (with ru+ro translations) and return their ids.

    Args:
        session: Active async session.
        attribute_id: Owning attribute id.
        labels: Map ``label -> {lang: translated_value}``.

    Returns:
        dict[str, int]: Map ``label -> attribute_value_id``.
    """
    ids: dict[str, int] = {}
    for label, translations in labels.items():
        value = AttributeValue(attribute_id=attribute_id)
        session.add(value)
        await session.flush()
        for lang, text in translations.items():
            session.add(
                AttributeValueTranslation(value_id=value.id, lang=lang, value=text)
            )
        ids[label] = value.id
    return ids


async def _seed_products(
    session: AsyncSession,
    count: int,
    leaf_ids: list[int],
    brand_ids: dict[str, int],
    color_ids: dict[str, int],
    size_ids: dict[str, int],
) -> list[int]:
    """Insert ``count`` active products (ru+ro, attributes, media) in batches.

    Args:
        session: Active async session.
        count: Number of products to create.
        leaf_ids: Leaf category ids to round-robin across.
        brand_ids: Map ``brand -> value_id``.
        color_ids: Map ``color -> value_id``.
        size_ids: Map ``size -> value_id``.

    Returns:
        list[int]: The ids of every created product.
    """
    brands = list(brand_ids.values())
    colors = list(color_ids.values())
    sizes = list(size_ids.values())
    product_ids: list[int] = []

    for index in range(count):
        code = f"BULK-{index:06d}"
        # Deterministic price spread so price sorts + facet bounds are meaningful.
        price = Decimal(1000 + (index % 900) * 10)
        # Every ~7th product carries an old_price (drives the "sale" badge/facet).
        old_price = price + Decimal(500) if index % 7 == 0 else None
        # A few zero-stock products so ``in_stock`` varies in the read-model.
        qty = 0 if index % 20 == 0 else (5 + index % 30)

        product = Product(
            category_id=leaf_ids[index % len(leaf_ids)],
            code=code,
            price=price,
            old_price=old_price,
            qty=qty,
            is_active=True,
        )
        session.add(product)
        await session.flush()
        product_ids.append(product.id)

        session.add_all(
            [
                ProductTranslation(
                    product_id=product.id,
                    lang="ru",
                    name=f"Товар {index}",
                    slug=f"tovar-{index}",
                    description=f"Описание товара {index} для нагрузочного теста.",
                ),
                ProductTranslation(
                    product_id=product.id,
                    lang="ro",
                    name=f"Produs {index}",
                    slug=f"produs-{index}",
                    description=f"Descrierea produsului {index} pentru testul de sarcină.",
                ),
            ]
        )

        session.add_all(
            [
                ProductAttribute(
                    product_id=product.id, value_id=brands[index % len(brands)]
                ),
                ProductAttribute(
                    product_id=product.id, value_id=colors[index % len(colors)]
                ),
                ProductAttribute(
                    product_id=product.id, value_id=sizes[index % len(sizes)]
                ),
            ]
        )

        session.add(
            Media(
                product_id=product.id,
                url=f"https://cdn.example/{code}-0.jpg",
                kind="image",
                position=0,
            )
        )
        if index % 3 == 0:
            session.add(
                Media(
                    product_id=product.id,
                    url=f"https://cdn.example/{code}-1.jpg",
                    kind="image",
                    position=1,
                )
            )

        if (index + 1) % BATCH_SIZE == 0:
            await session.commit()
            print(f"  ...inserted {index + 1}/{count} products")

    await session.commit()
    return product_ids


async def _rebuild_cards_batched(session: AsyncSession, product_ids: list[int]) -> int:
    """Rebuild ``product_card`` for all products, committing per batch.

    Args:
        session: Active async session.
        product_ids: Products whose read-model rows to (re)build.

    Returns:
        int: Total number of ``product_card`` rows written.
    """
    service = CatalogService(session)
    total = 0
    for start in range(0, len(product_ids), BATCH_SIZE):
        chunk = product_ids[start : start + BATCH_SIZE]
        total += await service.rebuild_cards(chunk)
        await session.commit()
        print(
            f"  ...rebuilt cards for {min(start + BATCH_SIZE, len(product_ids))}/{len(product_ids)}"
        )
    return total


async def seed_bulk(count: int) -> None:
    """Generate the benchmark catalog if it is not already populated.

    Args:
        count: Target number of active products.
    """
    async with session_factory() as session:
        existing = await session.scalar(
            select(func.count()).select_from(Product).where(Product.is_active.is_(True))
        )
        if existing and existing >= count:
            print(
                f"Catalog already holds {existing} active products (>= {count}) — nothing to do."
            )
            return

        started = time.perf_counter()
        leaf_ids = await _build_category_tree(session)
        brand_ids, color_ids, size_ids = await _build_attributes(session)
        await session.commit()

        print(f"Seeding {count} products across {len(leaf_ids)} categories...")
        product_ids = await _seed_products(
            session, count, leaf_ids, brand_ids, color_ids, size_ids
        )

        print("Rebuilding product_card read-model...")
        rebuilt = await _rebuild_cards_batched(session, product_ids)

        elapsed = time.perf_counter() - started
        print(
            f"Done: {len(product_ids)} products, {rebuilt} product_card rows "
            f"in {elapsed:.1f}s."
        )


if __name__ == "__main__":
    asyncio.run(seed_bulk(_parse_count()))
