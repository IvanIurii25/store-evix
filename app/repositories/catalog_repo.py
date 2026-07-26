"""Async data-access layer for the catalog read paths (stage B2).

Only SQL / query construction lives here — no business rules (those belong to
:class:`~app.services.catalog_service.CatalogService`). All queries are async
SQLAlchemy 2.0 ``select()`` statements.

Hot path (listing) reads the denormalized ``product_card`` (§5.2) and never
joins ``product ⨝ category ⨝ media``. Pagination is keyset/cursor (§5.3), not
OFFSET. Relations that must be materialized are loaded explicitly (no N+1).
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Select, and_, asc, delete, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cart import Cart, CartItem
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
    ProductCard,
    ProductTranslation,
    ProductVariant,
    ProductVariantValue,
    ProductVariationAttribute,
)


class CatalogRepository:
    """Read/write queries for catalog entities, bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used by every query method.

        Args:
            session: Active async session (request-scoped or test-scoped).
        """
        self.session = session

    # ------------------------------------------------------------------ #
    # Categories
    # ------------------------------------------------------------------ #
    async def list_active_categories(
        self,
        lang: str,
    ) -> list[tuple[Category, CategoryTranslation]]:
        """Return all active categories with their translation for ``lang``.

        The result is a flat list ordered by ``(depth, position, id)``; the
        service assembles the tree. Only categories that have a translation for
        the requested language are returned (publication rule, §2.1.1).

        Args:
            lang: Requested language code.

        Returns:
            list[tuple[Category, CategoryTranslation]]: Category + translation pairs.
        """
        stmt = (
            select(Category, CategoryTranslation)
            .join(
                CategoryTranslation,
                and_(
                    CategoryTranslation.category_id == Category.id,
                    CategoryTranslation.lang == lang,
                ),
            )
            .where(Category.is_active.is_(True))
            .order_by(Category.depth, Category.position, Category.id)
        )
        rows = await self.session.execute(stmt)
        return list(rows.all())

    async def get_category_by_slug(
        self,
        slug: str,
        lang: str,
    ) -> tuple[Category, CategoryTranslation] | None:
        """Fetch an active category by its per-language slug.

        Args:
            slug: Localized category slug.
            lang: Requested language code.

        Returns:
            tuple[Category, CategoryTranslation] | None: The pair, or ``None``.
        """
        stmt = (
            select(Category, CategoryTranslation)
            .join(
                CategoryTranslation,
                CategoryTranslation.category_id == Category.id,
            )
            .where(
                CategoryTranslation.lang == lang,
                CategoryTranslation.slug == slug,
                Category.is_active.is_(True),
            )
        )
        row = (await self.session.execute(stmt)).first()
        return tuple(row) if row is not None else None

    async def get_breadcrumb_translations(
        self,
        category_ids: list[int],
        lang: str,
    ) -> dict[int, CategoryTranslation]:
        """Load translations for the ids in a ``category.path`` (no recursion).

        Args:
            category_ids: Ancestor + self ids taken from ``category.path``.
            lang: Requested language code.

        Returns:
            dict[int, CategoryTranslation]: Map ``category_id -> translation``.
        """
        if not category_ids:
            return {}
        stmt = select(CategoryTranslation).where(
            CategoryTranslation.category_id.in_(category_ids),
            CategoryTranslation.lang == lang,
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.category_id: row for row in rows}

    async def get_child_categories(
        self,
        parent_id: int,
        lang: str,
    ) -> list[tuple[Category, CategoryTranslation]]:
        """Return direct active children of a category with their translation.

        Args:
            parent_id: Parent category id.
            lang: Requested language code.

        Returns:
            list[tuple[Category, CategoryTranslation]]: Child + translation pairs.
        """
        stmt = (
            select(Category, CategoryTranslation)
            .join(
                CategoryTranslation,
                and_(
                    CategoryTranslation.category_id == Category.id,
                    CategoryTranslation.lang == lang,
                ),
            )
            .where(
                Category.parent_id == parent_id,
                Category.is_active.is_(True),
            )
            .order_by(Category.position, Category.id)
        )
        rows = await self.session.execute(stmt)
        return list(rows.all())

    # ------------------------------------------------------------------ #
    # Listing (reads the denormalized product_card — §5.2, keyset — §5.3)
    # ------------------------------------------------------------------ #
    def _apply_listing_filters(
        self,
        stmt: Select,
        category_id: int | None,
        lang: str,
        price_min: Decimal | None,
        price_max: Decimal | None,
        value_ids: list[int] | None = None,
        on_sale: bool = False,
        featured: bool = False,
    ) -> Select:
        """Attach category-subtree, language, active, price and facet filters.

        ``category_id`` is optional: ``None`` lists across the whole store
        (homepage rails). ``on_sale`` keeps only cards with a struck-through
        ``old_price``; ``featured`` keeps only manually-curated cards.
        """
        stmt = stmt.where(
            ProductCard.lang == lang,
            ProductCard.is_active.is_(True),
        )
        # Subtree match: ``category_id`` appears anywhere in the card's ``path``.
        if category_id is not None:
            stmt = stmt.where(ProductCard.path.any(category_id))
        if on_sale:
            stmt = stmt.where(ProductCard.old_price.is_not(None))
        if featured:
            stmt = stmt.where(ProductCard.is_featured.is_(True))
        if price_min is not None:
            stmt = stmt.where(ProductCard.price >= price_min)
        if price_max is not None:
            stmt = stmt.where(ProductCard.price <= price_max)
        if value_ids:
            stmt = stmt.where(
                ProductCard.product_id.in_(self._attribute_filter_subquery(value_ids))
            )
        return stmt

    @staticmethod
    def _attribute_filter_subquery(value_ids: list[int]):
        """Products matching the selected attribute values (AND across attributes,
        OR within one attribute).

        A product qualifies when its selected-value hits span **every** distinct
        attribute present in the selection — i.e. at least one chosen value per
        chosen attribute. The expected distinct-attribute count is derived from
        the selection itself via a scalar subquery (no extra round-trip).
        """
        expected_attrs = (
            select(func.count(func.distinct(AttributeValue.attribute_id)))
            .where(AttributeValue.id.in_(value_ids))
            .scalar_subquery()
        )
        return (
            select(ProductAttribute.product_id)
            .join(AttributeValue, AttributeValue.id == ProductAttribute.value_id)
            .where(ProductAttribute.value_id.in_(value_ids))
            .group_by(ProductAttribute.product_id)
            .having(
                func.count(func.distinct(AttributeValue.attribute_id)) == expected_attrs
            )
        )

    async def list_cards(
        self,
        category_id: int | None,
        lang: str,
        order_columns: list,
        keyset_predicate,
        limit: int,
        price_min: Decimal | None = None,
        price_max: Decimal | None = None,
        value_ids: list[int] | None = None,
        on_sale: bool = False,
        featured: bool = False,
    ) -> list[ProductCard]:
        """Fetch one keyset page of listing cards from ``product_card``.

        Args:
            category_id: Category whose subtree is being listed.
            lang: Requested language code.
            order_columns: ORDER BY expressions defining a total order.
            keyset_predicate: Optional WHERE predicate for the cursor position
                (``None`` for the first page).
            limit: Maximum number of rows to return.
            price_min: Optional inclusive lower price bound.
            price_max: Optional inclusive upper price bound.

        Returns:
            list[ProductCard]: The page of cards (no joins — read-model only).
        """
        stmt = select(ProductCard)
        stmt = self._apply_listing_filters(
            stmt, category_id, lang, price_min, price_max, value_ids, on_sale, featured
        )
        if keyset_predicate is not None:
            stmt = stmt.where(keyset_predicate)
        stmt = stmt.order_by(*order_columns).limit(limit)
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(rows)

    async def get_card(self, product_id: int, lang: str) -> ProductCard | None:
        """Fetch a single ``product_card`` row for a product in one language.

        Args:
            product_id: Product primary key.
            lang: Requested language code.

        Returns:
            ProductCard | None: The card row, or ``None`` if the product is not
                published in that language (inactive / missing translation).
        """
        stmt = select(ProductCard).where(
            ProductCard.product_id == product_id,
            ProductCard.lang == lang,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_related_cards(
        self,
        category_id: int,
        lang: str,
        exclude_product_id: int,
        limit: int,
    ) -> list[ProductCard]:
        """Fetch in-stock sibling cards from one category, newest-first (§5.2).

        Reads the same denormalized ``product_card`` read-model as the listing
        (no joins), so related cards match listing cards field-for-field. Unlike
        the listing this also requires ``in_stock`` — cross-sell must be buyable —
        and matches ``category_id`` directly (the product's own category, not the
        subtree). One query, no N+1.

        Args:
            category_id: The current product's category.
            lang: Requested language code.
            exclude_product_id: The current product, excluded from its own siblings.
            limit: Maximum number of cards to return.

        Returns:
            list[ProductCard]: In-stock sibling cards ordered ``product_id`` DESC.
        """
        stmt = (
            select(ProductCard)
            .where(
                ProductCard.lang == lang,
                ProductCard.is_active.is_(True),
                ProductCard.in_stock.is_(True),
                ProductCard.category_id == category_id,
                ProductCard.product_id != exclude_product_id,
            )
            .order_by(desc(ProductCard.product_id))
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(rows)

    async def load_related_cards_by_ids(
        self,
        product_ids: list[int],
        lang: str,
        exclude_product_id: int,
    ) -> dict[int, ProductCard]:
        """Batch-hydrate buyable cards for the given ids in one query (no N+1).

        Hydrates the ES-ranked candidate ids for cross-sell: reads the same
        ``product_card`` read-model as the listing, keeping only ``is_active``
        **and** ``in_stock`` rows (cross-sell must be buyable — the authoritative
        stock check ES cannot do). Returned as a map so the caller can preserve
        the ES relevance order; missing / OOS / other-lang ids are simply absent.

        Args:
            product_ids: Candidate product ids (in any order).
            lang: Request language code (``product_card`` is keyed by lang).
            exclude_product_id: The seed product, always excluded from its own rail.

        Returns:
            dict[int, ProductCard]: Map ``product_id -> card`` for buyable cards.
        """
        if not product_ids:
            return {}
        stmt = select(ProductCard).where(
            ProductCard.lang == lang,
            ProductCard.is_active.is_(True),
            ProductCard.in_stock.is_(True),
            ProductCard.product_id.in_(product_ids),
            ProductCard.product_id != exclude_product_id,
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {card.product_id: card for card in rows}

    @staticmethod
    def order_columns_for(sort: str) -> list:
        """Return ORDER BY expressions for a sort key (total order via product_id)."""
        if sort == "price_asc":
            return [asc(ProductCard.price), asc(ProductCard.product_id)]
        if sort == "price_desc":
            return [desc(ProductCard.price), desc(ProductCard.product_id)]
        # newest: higher product_id == newer (monotonic bigint PK)
        return [desc(ProductCard.product_id)]

    @staticmethod
    def keyset_predicate_for(
        sort: str,
        last_product_id: int,
        last_price: Decimal | None,
    ):
        """Build the keyset WHERE predicate continuing after the last row.

        Args:
            sort: Sort key (``newest`` / ``price_asc`` / ``price_desc``).
            last_product_id: ``product_id`` of the last row on the previous page.
            last_price: ``price`` of that row (used by price sorts).

        Returns:
            A SQLAlchemy boolean expression for the cursor position.
        """
        if sort == "price_asc":
            return or_(
                ProductCard.price > last_price,
                and_(
                    ProductCard.price == last_price,
                    ProductCard.product_id > last_product_id,
                ),
            )
        if sort == "price_desc":
            return or_(
                ProductCard.price < last_price,
                and_(
                    ProductCard.price == last_price,
                    ProductCard.product_id < last_product_id,
                ),
            )
        return ProductCard.product_id < last_product_id

    # ------------------------------------------------------------------ #
    # Product detail (PDP)
    # ------------------------------------------------------------------ #
    async def get_product_translation_by_slug(
        self,
        slug: str,
        lang: str,
    ) -> ProductTranslation | None:
        """Resolve a product translation by its per-language slug.

        Args:
            slug: Localized product slug.
            lang: Requested language code.

        Returns:
            ProductTranslation | None: The matching translation, or ``None``.
        """
        stmt = select(ProductTranslation).where(
            ProductTranslation.lang == lang,
            ProductTranslation.slug == slug,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_product(self, product_id: int) -> Product | None:
        """Fetch a product row by id.

        Args:
            product_id: Product primary key.

        Returns:
            Product | None: The product, or ``None`` if absent.
        """
        stmt = select(Product).where(Product.id == product_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_product_translations(
        self,
        product_id: int,
    ) -> list[ProductTranslation]:
        """Return all translations of a product (both languages, for slugs).

        Args:
            product_id: Product primary key.

        Returns:
            list[ProductTranslation]: All translation rows for the product.
        """
        stmt = select(ProductTranslation).where(
            ProductTranslation.product_id == product_id
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_category_translations(
        self,
        category_id: int,
    ) -> list[CategoryTranslation]:
        """Return all translations of a category (both languages, for slugs).

        Args:
            category_id: Category primary key.

        Returns:
            list[CategoryTranslation]: All translation rows for the category.
        """
        stmt = select(CategoryTranslation).where(
            CategoryTranslation.category_id == category_id
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_active_category_slugs(
        self,
    ) -> list[tuple[int, datetime, str, str]]:
        """Return ``(id, updated_at, lang, slug)`` for every active category.

        One row per translation; the service groups by id into per-language
        slug sets for the sitemap. Ordered by id for stable output.
        """
        stmt = (
            select(
                Category.id,
                Category.updated_at,
                CategoryTranslation.lang,
                CategoryTranslation.slug,
            )
            .join(CategoryTranslation, CategoryTranslation.category_id == Category.id)
            .where(Category.is_active.is_(True))
            .order_by(Category.id)
        )
        return list((await self.session.execute(stmt)).all())

    async def get_active_product_slugs(
        self,
    ) -> list[tuple[int, datetime, str, str]]:
        """Return ``(id, updated_at, lang, slug)`` for every active product."""
        stmt = (
            select(
                Product.id,
                Product.updated_at,
                ProductTranslation.lang,
                ProductTranslation.slug,
            )
            .join(ProductTranslation, ProductTranslation.product_id == Product.id)
            .where(Product.is_active.is_(True))
            .order_by(Product.id)
        )
        return list((await self.session.execute(stmt)).all())

    async def get_product_media(self, product_id: int) -> list[Media]:
        """Return a product's media ordered by ``position`` (no N+1).

        Args:
            product_id: Product primary key.

        Returns:
            list[Media]: Ordered media rows.
        """
        stmt = (
            select(Media)
            .where(Media.product_id == product_id)
            .order_by(Media.position, Media.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_live_carts_with_product(self, product_id: int) -> int:
        """Count distinct live carts currently holding a product (honest signal).

        "Live" = a cart in ``draft`` status (an active, not-yet-converted and
        not-abandoned cart). One cheap aggregate: ``COUNT(DISTINCT cart_id)`` over
        ``cart_item`` joined to ``cart``. No N+1 — one round-trip per PDP.

        Args:
            product_id: Product primary key.

        Returns:
            int: Number of distinct live carts containing the product (``0`` if
                none). This is a *real* count — never randomized or inflated.
        """
        stmt = (
            select(func.count(func.distinct(CartItem.cart_id)))
            .join(Cart, Cart.id == CartItem.cart_id)
            .where(
                CartItem.product_id == product_id,
                Cart.status == "draft",
            )
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def get_product_attributes(
        self,
        product_id: int,
        lang: str,
    ) -> list[tuple[str, str, str]]:
        """Return a product's characteristics as ``(code, attr_name, value)``.

        Joins the M2M ``product_attribute`` chain to the attribute dictionary and
        localized value in a single query (no N+1 across attributes).

        Args:
            product_id: Product primary key.
            lang: Requested language code.

        Returns:
            list[tuple[str, str, str]]: ``(attribute_code, attribute_name, value)``
                rows ordered by attribute code.
        """
        stmt = (
            select(
                Attribute.code,
                AttributeTranslation.name,
                AttributeValueTranslation.value,
            )
            .join(
                AttributeValue,
                AttributeValue.attribute_id == Attribute.id,
            )
            .join(
                ProductAttribute,
                ProductAttribute.value_id == AttributeValue.id,
            )
            .join(
                AttributeTranslation,
                and_(
                    AttributeTranslation.attribute_id == Attribute.id,
                    AttributeTranslation.lang == lang,
                ),
            )
            .join(
                AttributeValueTranslation,
                and_(
                    AttributeValueTranslation.value_id == AttributeValue.id,
                    AttributeValueTranslation.lang == lang,
                ),
            )
            .where(ProductAttribute.product_id == product_id)
            .order_by(Attribute.code, AttributeValueTranslation.value)
        )
        rows = await self.session.execute(stmt)
        return [tuple(row) for row in rows.all()]

    # ------------------------------------------------------------------ #
    # variants (variable products)
    # ------------------------------------------------------------------ #
    async def get_variant_aggregate(
        self, product_id: int
    ) -> tuple[Decimal | None, Decimal | None, bool, bool, int]:
        """Aggregate a product's ACTIVE variants for the card read-model.

        Args:
            product_id: Product primary key.

        Returns:
            tuple: ``(min_price, max_price, any_in_stock, any_on_sale, total_qty)``.
                ``(None, None, False, False, 0)`` when there are no active variants.
        """
        stmt = select(
            func.min(ProductVariant.price),
            func.max(ProductVariant.price),
            func.bool_or(ProductVariant.qty > 0),
            func.bool_or(
                and_(
                    ProductVariant.old_price.is_not(None),
                    ProductVariant.old_price > ProductVariant.price,
                )
            ),
            func.coalesce(func.sum(ProductVariant.qty), 0),
        ).where(
            ProductVariant.product_id == product_id,
            ProductVariant.is_active.is_(True),
        )
        row = (await self.session.execute(stmt)).one()
        return row[0], row[1], bool(row[2]), bool(row[3]), int(row[4])

    async def get_variation_attributes(
        self, product_id: int, lang: str
    ) -> list[tuple[int, str, str, int, int, str]]:
        """Return variation-selector options for a product (no N+1).

        One row per ``(attribute, value)`` actually present in the product's
        active variants, ordered by selector position then value.

        Args:
            product_id: Product primary key.
            lang: Requested language code.

        Returns:
            list[tuple]: ``(attribute_id, code, name, position, value_id, value)``.
        """
        stmt = (
            select(
                Attribute.id,
                Attribute.code,
                AttributeTranslation.name,
                ProductVariationAttribute.position,
                AttributeValue.id,
                AttributeValueTranslation.value,
            )
            .join(
                ProductVariationAttribute,
                ProductVariationAttribute.attribute_id == Attribute.id,
            )
            .join(AttributeValue, AttributeValue.attribute_id == Attribute.id)
            .join(
                ProductVariantValue,
                ProductVariantValue.value_id == AttributeValue.id,
            )
            .join(
                ProductVariant,
                and_(
                    ProductVariant.id == ProductVariantValue.variant_id,
                    ProductVariant.product_id == product_id,
                    ProductVariant.is_active.is_(True),
                ),
            )
            .join(
                AttributeTranslation,
                and_(
                    AttributeTranslation.attribute_id == Attribute.id,
                    AttributeTranslation.lang == lang,
                ),
            )
            .join(
                AttributeValueTranslation,
                and_(
                    AttributeValueTranslation.value_id == AttributeValue.id,
                    AttributeValueTranslation.lang == lang,
                ),
            )
            .where(ProductVariationAttribute.product_id == product_id)
            .order_by(
                ProductVariationAttribute.position,
                Attribute.code,
                AttributeValueTranslation.value,
            )
            .distinct()
        )
        rows = await self.session.execute(stmt)
        return [tuple(row) for row in rows.all()]

    async def get_product_variants(self, product_id: int) -> list[ProductVariant]:
        """Return a product's ACTIVE variants ordered by ``position`` (no N+1)."""
        stmt = (
            select(ProductVariant)
            .where(
                ProductVariant.product_id == product_id,
                ProductVariant.is_active.is_(True),
            )
            .order_by(ProductVariant.position, ProductVariant.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_variant_value_ids(
        self, variant_ids: list[int]
    ) -> dict[int, list[int]]:
        """Return ``{variant_id: [value_id, ...]}`` for the given variants."""
        if not variant_ids:
            return {}
        stmt = (
            select(ProductVariantValue.variant_id, ProductVariantValue.value_id)
            .where(ProductVariantValue.variant_id.in_(variant_ids))
            .order_by(ProductVariantValue.variant_id, ProductVariantValue.value_id)
        )
        result: dict[int, list[int]] = {}
        for variant_id, value_id in (await self.session.execute(stmt)).all():
            result.setdefault(variant_id, []).append(value_id)
        return result

    async def get_variant_image_urls(self, variant_ids: list[int]) -> dict[int, str]:
        """Return ``{variant_id: main_image_url}`` (lowest-position media each)."""
        if not variant_ids:
            return {}
        stmt = (
            select(Media.variant_id, Media.url)
            .where(Media.variant_id.in_(variant_ids))
            .order_by(Media.variant_id, Media.position, Media.id)
        )
        result: dict[int, str] = {}
        for variant_id, url in (await self.session.execute(stmt)).all():
            if variant_id not in result:  # first seen = lowest position
                result[variant_id] = url
        return result

    async def get_variant(self, variant_id: int) -> ProductVariant | None:
        """Fetch one variant row by id (buy-path validation)."""
        return await self.session.get(ProductVariant, variant_id)

    async def get_variants_by_ids(
        self, variant_ids: list[int]
    ) -> dict[int, ProductVariant]:
        """Return ``{id: ProductVariant}`` for the given variant ids (no N+1)."""
        if not variant_ids:
            return {}
        stmt = select(ProductVariant).where(ProductVariant.id.in_(variant_ids))
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.id: row for row in rows}

    async def get_variant_option_labels(
        self, variant_ids: list[int], lang: str
    ) -> dict[int, str]:
        """Return ``{variant_id: "Bej, 40×60"}`` — localized options, in selector
        order (variation-attribute position), joined for display/snapshot.
        """
        if not variant_ids:
            return {}
        stmt = (
            select(
                ProductVariantValue.variant_id,
                AttributeValueTranslation.value,
            )
            .join(
                AttributeValue,
                AttributeValue.id == ProductVariantValue.value_id,
            )
            .join(
                AttributeValueTranslation,
                and_(
                    AttributeValueTranslation.value_id == AttributeValue.id,
                    AttributeValueTranslation.lang == lang,
                ),
            )
            .join(
                ProductVariant,
                ProductVariant.id == ProductVariantValue.variant_id,
            )
            .join(
                ProductVariationAttribute,
                and_(
                    ProductVariationAttribute.product_id == ProductVariant.product_id,
                    ProductVariationAttribute.attribute_id
                    == AttributeValue.attribute_id,
                ),
            )
            .where(ProductVariantValue.variant_id.in_(variant_ids))
            .order_by(
                ProductVariantValue.variant_id,
                ProductVariationAttribute.position,
                AttributeValue.id,
            )
        )
        parts: dict[int, list[str]] = {}
        for variant_id, value in (await self.session.execute(stmt)).all():
            parts.setdefault(variant_id, []).append(value)
        return {vid: ", ".join(vals) for vid, vals in parts.items()}

    # ------------------------------------------------------------------ #
    # product_card read-model (§5.2) write side
    # ------------------------------------------------------------------ #
    async def get_category(self, category_id: int) -> Category | None:
        """Fetch a category row by id (used when rebuilding a card's ``path``).

        Args:
            category_id: Category primary key.

        Returns:
            Category | None: The category, or ``None``.
        """
        stmt = select(Category).where(Category.id == category_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_main_image_url(self, product_id: int) -> str | None:
        """Return the lowest-``position`` image url for a product, if any.

        Args:
            product_id: Product primary key.

        Returns:
            str | None: The main image url, or ``None`` when the product has none.
        """
        stmt = (
            select(Media.url)
            .where(Media.product_id == product_id)
            .order_by(Media.position, Media.id)
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def count_product_translations(self, product_id: int) -> int:
        """Count how many language translations a product has.

        Args:
            product_id: Product primary key.

        Returns:
            int: Number of translation rows.
        """
        stmt = (
            select(func.count())
            .select_from(ProductTranslation)
            .where(ProductTranslation.product_id == product_id)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def delete_product_cards(self, product_id: int) -> None:
        """Remove all ``product_card`` rows for a product (before rebuild).

        Args:
            product_id: Product primary key.
        """
        stmt = delete(ProductCard).where(ProductCard.product_id == product_id)
        await self.session.execute(stmt)

    async def upsert_card(self, card: ProductCard) -> None:
        """Add a freshly-built ``product_card`` row to the session.

        Args:
            card: The card instance to persist.
        """
        self.session.add(card)
