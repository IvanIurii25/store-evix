"""Dev seed: a small catalog (<=10 products) with ru+ro translations.

Run against a migrated DB:  ``uv run python scripts/seed.py``

Idempotent: does nothing if products already exist. Populates categories,
attributes/values (both langs), products (both langs), media, and rebuilds the
``product_card`` read-model via :class:`CatalogService`.
"""

import asyncio
from decimal import Decimal

from sqlalchemy import select

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

# (code, price, qty, {lang: (name, slug)}, brand, color)
PRODUCTS = [
    (
        "PH-001",
        "12999.00",
        8,
        {
            "ru": ("Смартфон Galaxy S", "smartfon-galaxy-s"),
            "ro": ("Smartphone Galaxy S", "smartphone-galaxy-s"),
        },
        "Samsung",
        "Black",
    ),
    (
        "PH-002",
        "18999.00",
        5,
        {
            "ru": ("Смартфон iPhone", "smartfon-iphone"),
            "ro": ("Smartphone iPhone", "smartphone-iphone"),
        },
        "Apple",
        "White",
    ),
    (
        "PH-003",
        "9999.00",
        12,
        {
            "ru": ("Смартфон Galaxy A", "smartfon-galaxy-a"),
            "ro": ("Smartphone Galaxy A", "smartphone-galaxy-a"),
        },
        "Samsung",
        "White",
    ),
    (
        "TB-001",
        "15999.00",
        3,
        {
            "ru": ("Планшет Tab S", "planshet-tab-s"),
            "ro": ("Tabletă Tab S", "tableta-tab-s"),
        },
        "Samsung",
        "Black",
    ),
    (
        "TB-002",
        "22999.00",
        0,
        {
            "ru": ("Планшет iPad", "planshet-ipad"),
            "ro": ("Tabletă iPad", "tableta-ipad"),
        },
        "Apple",
        "Black",
    ),
    (
        "WT-001",
        "4999.00",
        20,
        {"ru": ("Часы Watch", "chasy-watch"), "ro": ("Ceas Watch", "ceas-watch")},
        "Apple",
        "White",
    ),
]


async def _get_or_create_value(session, cache, attr_id, code, names):
    key = (attr_id, code)
    if key in cache:
        return cache[key]
    value = AttributeValue(attribute_id=attr_id)
    session.add(value)
    await session.flush()
    for lang, text in names.items():
        session.add(AttributeValueTranslation(value_id=value.id, lang=lang, value=text))
    cache[key] = value.id
    return value.id


async def seed() -> None:
    async with session_factory() as session:
        if await session.scalar(select(Product.id).limit(1)):
            print("Catalog already seeded — nothing to do.")
            return

        # --- Category tree: Электроника > Смартфоны ---
        root = Category(path=[], depth=0, is_active=True, position=0)
        session.add(root)
        await session.flush()
        root.path = [root.id]
        session.add_all(
            [
                CategoryTranslation(
                    category_id=root.id,
                    lang="ru",
                    name="Электроника",
                    slug="elektronika",
                ),
                CategoryTranslation(
                    category_id=root.id,
                    lang="ro",
                    name="Electronice",
                    slug="electronice",
                ),
            ]
        )

        leaf = Category(path=[], depth=1, is_active=True, position=0, parent_id=root.id)
        session.add(leaf)
        await session.flush()
        leaf.path = [root.id, leaf.id]
        session.add_all(
            [
                CategoryTranslation(
                    category_id=leaf.id, lang="ru", name="Гаджеты", slug="gadzhety"
                ),
                CategoryTranslation(
                    category_id=leaf.id, lang="ro", name="Gadgeturi", slug="gadgeturi"
                ),
            ]
        )

        # --- Attributes: brand, color ---
        brand = Attribute(code="brand")
        color = Attribute(code="color")
        session.add_all([brand, color])
        await session.flush()
        session.add_all(
            [
                AttributeTranslation(attribute_id=brand.id, lang="ru", name="Бренд"),
                AttributeTranslation(attribute_id=brand.id, lang="ro", name="Brand"),
                AttributeTranslation(attribute_id=color.id, lang="ru", name="Цвет"),
                AttributeTranslation(attribute_id=color.id, lang="ro", name="Culoare"),
            ]
        )

        val_cache: dict[tuple[int, str], int] = {}
        color_names = {
            "Black": {"ru": "Чёрный", "ro": "Negru"},
            "White": {"ru": "Белый", "ro": "Alb"},
        }

        product_ids: list[int] = []
        for code, price, qty, names, brand_val, color_val in PRODUCTS:
            product = Product(
                category_id=leaf.id,
                code=code,
                price=Decimal(price),
                qty=qty,
                is_active=True,
            )
            session.add(product)
            await session.flush()
            product_ids.append(product.id)

            for lang, (name, slug) in names.items():
                session.add(
                    ProductTranslation(
                        product_id=product.id,
                        lang=lang,
                        name=name,
                        slug=slug,
                        description=f"{name} — demo seed.",
                    )
                )
            session.add(
                Media(
                    product_id=product.id,
                    url=f"https://cdn.example/{code}.jpg",
                    kind="image",
                    position=0,
                )
            )

            brand_vid = await _get_or_create_value(
                session,
                val_cache,
                brand.id,
                brand_val,
                {"ru": brand_val, "ro": brand_val},
            )
            color_vid = await _get_or_create_value(
                session, val_cache, color.id, color_val, color_names[color_val]
            )
            session.add_all(
                [
                    ProductAttribute(product_id=product.id, value_id=brand_vid),
                    ProductAttribute(product_id=product.id, value_id=color_vid),
                ]
            )

        await session.commit()

        # --- Rebuild the denormalized product_card read-model ---
        rebuilt = await CatalogService(session).rebuild_cards(product_ids)
        await session.commit()
        print(f"Seeded {len(product_ids)} products, {rebuilt} product_card rows.")


if __name__ == "__main__":
    asyncio.run(seed())
