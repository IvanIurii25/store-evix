"""Catalog business logic (stage B2).

Holds the rules the router and repository must not: tree assembly from a flat
category list, breadcrumb resolution from ``category.path`` (no recursion, §2.1.1),
keyset-cursor listing over the ``product_card`` read-model (§5.2/§5.3), PDP
assembly, the ``product_card`` rebuild, and the publication rule — activation
requires translations in **both** languages (§2.1.1).

The service knows nothing about HTTP; it raises domain errors that the router
maps to responses.
"""

import base64
import binascii
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import ALLOWED_LANGS, ProductCard
from app.repositories.catalog_repo import CatalogRepository
from app.schemas.catalog import (
    AttributeGroup,
    Breadcrumb,
    CategoryDetail,
    CategoryNode,
    CategorySlug,
    ProductCardOut,
    ProductDetail,
    ProductListing,
    ProductSlug,
    SitemapData,
    SitemapEntry,
    SitemapSlug,
)

# Listing page size for keyset pagination (§5.3).
DEFAULT_PAGE_SIZE: int = 24
MAX_PAGE_SIZE: int = 100

# Sort keys accepted by the listing endpoint (§4).
PRICE_SORTS: frozenset[str] = frozenset({"price_asc", "price_desc"})


class CatalogError(Exception):
    """Base class for catalog domain errors (mapped to HTTP by the router)."""


class NotFoundError(CatalogError):
    """A requested catalog resource does not exist / is not published."""


class PublicationError(CatalogError):
    """A product cannot be published: the both-languages rule is unmet (§2.1.1)."""


class InvalidCursorError(CatalogError):
    """The supplied listing cursor is malformed."""


def _encode_cursor(sort: str, product_id: int, price: Decimal | None) -> str:
    """Serialize the keyset position into an opaque base64 token.

    Args:
        sort: Sort key the cursor belongs to.
        product_id: ``product_id`` of the last row on the page.
        price: ``price`` of that row (needed by price sorts).

    Returns:
        str: URL-safe base64 cursor token.
    """
    payload = {
        "s": sort,
        "id": product_id,
        "p": str(price) if price is not None else None,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str, sort: str) -> tuple[int, Decimal | None]:
    """Parse an opaque cursor and validate it against the current sort.

    Args:
        cursor: The token produced by :func:`_encode_cursor`.
        sort: The sort key of the current request.

    Returns:
        tuple[int, Decimal | None]: ``(last_product_id, last_price)``.

    Raises:
        InvalidCursorError: If the token is malformed or its sort mismatches.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw)
        cursor_sort = payload["s"]
        last_id = int(payload["id"])
        last_price = Decimal(payload["p"]) if payload["p"] is not None else None
    except (
        binascii.Error,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        InvalidOperation,
    ) as exc:
        raise InvalidCursorError("Malformed listing cursor") from exc
    if cursor_sort != sort:
        raise InvalidCursorError("Cursor does not match the requested sort order")
    return last_id, last_price


class CatalogService:
    """Read + read-model-write operations for the catalog."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and its repository.

        Args:
            session: Active async session used for reads and card rebuilds.
        """
        self.session = session
        self.repo = CatalogRepository(session)

    # ------------------------------------------------------------------ #
    # Categories
    # ------------------------------------------------------------------ #
    async def list_categories(self, lang: str) -> list[CategoryNode]:
        """Return the active category tree for a language.

        Args:
            lang: Requested language code.

        Returns:
            list[CategoryNode]: Root nodes with nested ``children``.
        """
        pairs = await self.repo.list_active_categories(lang)
        nodes: dict[int, CategoryNode] = {}
        for category, translation in pairs:
            nodes[category.id] = CategoryNode(
                id=category.id,
                parent_id=category.parent_id,
                name=translation.name,
                slug=translation.slug,
                depth=category.depth,
                position=category.position,
            )
        roots: list[CategoryNode] = []
        for node in nodes.values():
            parent = nodes.get(node.parent_id) if node.parent_id is not None else None
            if parent is not None:
                parent.children.append(node)
            else:
                roots.append(node)
        return roots

    async def get_category(self, slug: str, lang: str) -> CategoryDetail:
        """Return category detail: name/slug + breadcrumbs + children + seo.

        Args:
            slug: Localized category slug.
            lang: Requested language code.

        Returns:
            CategoryDetail: The assembled detail response.

        Raises:
            NotFoundError: If no active category has that slug in the language.
        """
        found = await self.repo.get_category_by_slug(slug, lang)
        if found is None:
            raise NotFoundError(f"Category '{slug}' not found")
        category, translation = found

        breadcrumbs = await self._build_breadcrumbs(category.path, lang)
        child_pairs = await self.repo.get_child_categories(category.id, lang)
        children = [
            CategoryNode(
                id=child.id,
                parent_id=child.parent_id,
                name=child_tr.name,
                slug=child_tr.slug,
                depth=child.depth,
                position=child.position,
            )
            for child, child_tr in child_pairs
        ]
        all_translations = await self.repo.get_category_translations(category.id)
        slugs = [CategorySlug(lang=tr.lang, slug=tr.slug) for tr in all_translations]
        return CategoryDetail(
            id=category.id,
            name=translation.name,
            slug=translation.slug,
            seo_title=translation.seo_title,
            seo_description=translation.seo_description,
            breadcrumbs=breadcrumbs,
            children=children,
            slugs=slugs,
        )

    async def _build_breadcrumbs(
        self,
        path: list[int],
        lang: str,
    ) -> list[Breadcrumb]:
        """Resolve ``category.path`` ids into ordered breadcrumbs (no recursion).

        Args:
            path: Ancestor..self category ids from ``category.path``.
            lang: Requested language code.

        Returns:
            list[Breadcrumb]: Breadcrumbs in root..self order, skipping ids that
                have no translation in the language.
        """
        translations = await self.repo.get_breadcrumb_translations(path, lang)
        crumbs: list[Breadcrumb] = []
        for category_id in path:
            translation = translations.get(category_id)
            if translation is not None:
                crumbs.append(
                    Breadcrumb(
                        id=category_id,
                        name=translation.name,
                        slug=translation.slug,
                    )
                )
        return crumbs

    # ------------------------------------------------------------------ #
    # Listing (product_card read-model, keyset cursor)
    # ------------------------------------------------------------------ #
    async def list_products(
        self,
        category_slug: str,
        lang: str,
        sort: str = "newest",
        cursor: str | None = None,
        price_min: Decimal | None = None,
        price_max: Decimal | None = None,
        value_ids: list[int] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> ProductListing:
        """List a category's products from the read-model, keyset-paginated.

        Args:
            category_slug: Localized slug of the category to list.
            lang: Requested language code.
            sort: ``newest`` / ``price_asc`` / ``price_desc``.
            cursor: Opaque cursor from a previous page (``None`` = first page).
            price_min: Optional inclusive lower price bound.
            price_max: Optional inclusive upper price bound.
            page_size: Requested page size (clamped to ``MAX_PAGE_SIZE``).

        Returns:
            ProductListing: ``{data, next_cursor}`` envelope.

        Raises:
            NotFoundError: If the category slug is unknown in the language.
            InvalidCursorError: If ``cursor`` is malformed or sort-mismatched.
        """
        found = await self.repo.get_category_by_slug(category_slug, lang)
        if found is None:
            raise NotFoundError(f"Category '{category_slug}' not found")
        category, _ = found

        return await self._list_cards_page(
            category_id=category.id,
            lang=lang,
            sort=sort,
            cursor=cursor,
            price_min=price_min,
            price_max=price_max,
            value_ids=value_ids,
            page_size=page_size,
        )

    async def list_all_products(
        self,
        lang: str,
        sort: str = "newest",
        cursor: str | None = None,
        price_min: Decimal | None = None,
        price_max: Decimal | None = None,
        on_sale: bool = False,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> ProductListing:
        """List products across the whole store, keyset-paginated (homepage rails).

        Same read-model + cursor as :meth:`list_products` but without a category
        constraint. Powers the store-wide "newest" and "on sale" rails.

        Args:
            lang: Requested language code.
            sort: ``newest`` / ``price_asc`` / ``price_desc``.
            cursor: Opaque cursor from a previous page (``None`` = first page).
            price_min: Optional inclusive lower price bound.
            price_max: Optional inclusive upper price bound.
            on_sale: Keep only products with a struck-through ``old_price``.
            page_size: Requested page size (clamped to ``MAX_PAGE_SIZE``).

        Returns:
            ProductListing: ``{data, next_cursor}`` envelope.

        Raises:
            InvalidCursorError: If ``cursor`` is malformed or sort-mismatched.
        """
        return await self._list_cards_page(
            category_id=None,
            lang=lang,
            sort=sort,
            cursor=cursor,
            price_min=price_min,
            price_max=price_max,
            value_ids=None,
            on_sale=on_sale,
            page_size=page_size,
        )

    async def _list_cards_page(
        self,
        category_id: int | None,
        lang: str,
        sort: str,
        cursor: str | None,
        price_min: Decimal | None,
        price_max: Decimal | None,
        value_ids: list[int] | None,
        on_sale: bool = False,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> ProductListing:
        """Shared keyset-pagination core over ``product_card`` (§5.3).

        Args:
            category_id: Subtree to list, or ``None`` for the whole store.
            lang: Requested language code.
            sort: Sort key.
            cursor: Opaque cursor (``None`` = first page).
            price_min: Optional inclusive lower price bound.
            price_max: Optional inclusive upper price bound.
            value_ids: Optional facet selection (category listings only).
            on_sale: Keep only products with an ``old_price``.
            page_size: Requested page size (clamped to ``MAX_PAGE_SIZE``).

        Returns:
            ProductListing: ``{data, next_cursor}`` envelope.

        Raises:
            InvalidCursorError: If ``cursor`` is malformed or sort-mismatched.
        """
        limit = max(1, min(page_size, MAX_PAGE_SIZE))
        keyset_predicate = None
        if cursor is not None:
            last_id, last_price = _decode_cursor(cursor, sort)
            keyset_predicate = self.repo.keyset_predicate_for(sort, last_id, last_price)

        order_columns = self.repo.order_columns_for(sort)
        cards = await self.repo.list_cards(
            category_id=category_id,
            lang=lang,
            order_columns=order_columns,
            keyset_predicate=keyset_predicate,
            limit=limit + 1,
            price_min=price_min,
            price_max=price_max,
            value_ids=value_ids,
            on_sale=on_sale,
        )

        has_more = len(cards) > limit
        page = cards[:limit]
        next_cursor: str | None = None
        if has_more and page:
            last = page[-1]
            price = last.price if sort in PRICE_SORTS else None
            next_cursor = _encode_cursor(sort, last.product_id, price)

        return ProductListing(
            data=[ProductCardOut.model_validate(card) for card in page],
            next_cursor=next_cursor,
        )

    # ------------------------------------------------------------------ #
    # Product detail (PDP)
    # ------------------------------------------------------------------ #
    async def get_product(self, slug: str, lang: str) -> ProductDetail:
        """Assemble the product detail page for a slug + language.

        Args:
            slug: Localized product slug.
            lang: Requested language code.

        Returns:
            ProductDetail: Product + grouped attributes + media + both slugs.

        Raises:
            NotFoundError: If no active product has that slug, or it is inactive.
        """
        translation = await self.repo.get_product_translation_by_slug(slug, lang)
        if translation is None:
            raise NotFoundError(f"Product '{slug}' not found")
        product = await self.repo.get_product(translation.product_id)
        if product is None or not product.is_active:
            raise NotFoundError(f"Product '{slug}' not found")

        media = await self.repo.get_product_media(product.id)
        attribute_rows = await self.repo.get_product_attributes(product.id, lang)
        attributes = self._group_attributes(attribute_rows)
        all_translations = await self.repo.get_product_translations(product.id)
        slugs = [ProductSlug(lang=tr.lang, slug=tr.slug) for tr in all_translations]
        category = await self.repo.get_category(product.category_id)
        breadcrumbs: list[Breadcrumb] = []
        if category is not None:
            breadcrumbs = await self._build_breadcrumbs(category.path, lang)

        return ProductDetail(
            id=product.id,
            code=product.code,
            name=translation.name,
            slug=translation.slug,
            description=translation.description,
            seo_title=translation.seo_title,
            seo_description=translation.seo_description,
            price=product.price,
            old_price=product.old_price,
            in_stock=product.qty > 0,
            category_id=product.category_id,
            breadcrumbs=breadcrumbs,
            attributes=attributes,
            media=media,
            slugs=slugs,
        )

    async def get_sitemap(self) -> SitemapData:
        """Return every published category + product with per-language slugs.

        Flat feed for building per-locale sitemaps with hreflang alternates in a
        single request (two grouped queries, no per-entity round-trips).

        Returns:
            SitemapData: Published categories and products, each with its
                ``(lang, slug)`` pairs and ``updated_at`` for ``lastmod``.
        """
        category_rows = await self.repo.get_active_category_slugs()
        product_rows = await self.repo.get_active_product_slugs()
        return SitemapData(
            categories=self._group_sitemap(category_rows),
            products=self._group_sitemap(product_rows),
        )

    @staticmethod
    def _group_sitemap(
        rows: list[tuple[int, datetime, str, str]],
    ) -> list[SitemapEntry]:
        """Group ``(id, updated_at, lang, slug)`` rows into one entry per entity."""
        entries: dict[int, SitemapEntry] = {}
        for entity_id, updated_at, lang, slug in rows:
            entry = entries.get(entity_id)
            if entry is None:
                entry = SitemapEntry(slugs=[], updated_at=updated_at)
                entries[entity_id] = entry
            entry.slugs.append(SitemapSlug(lang=lang, slug=slug))
        return list(entries.values())

    @staticmethod
    def _group_attributes(
        rows: list[tuple[str, str, str]],
    ) -> list[AttributeGroup]:
        """Group ``(code, name, value)`` rows into one entry per attribute.

        Args:
            rows: ``(attribute_code, attribute_name, value)`` in code order.

        Returns:
            list[AttributeGroup]: One group per attribute, values collected.
        """
        grouped: dict[str, AttributeGroup] = {}
        for code, name, value in rows:
            group = grouped.get(code)
            if group is None:
                group = AttributeGroup(code=code, name=name, values=[])
                grouped[code] = group
            group.values.append(value)
        return list(grouped.values())

    # ------------------------------------------------------------------ #
    # product_card read-model rebuild + publication rule (§5.2 / §2.1.1)
    # ------------------------------------------------------------------ #
    async def assert_publishable(self, product_id: int) -> None:
        """Enforce the publication rule: both language translations must exist.

        Args:
            product_id: Product to validate.

        Raises:
            NotFoundError: If the product does not exist.
            PublicationError: If translations for all languages are not present.
        """
        product = await self.repo.get_product(product_id)
        if product is None:
            raise NotFoundError(f"Product {product_id} not found")
        translations = await self.repo.get_product_translations(product_id)
        present = {tr.lang for tr in translations}
        missing = set(ALLOWED_LANGS) - present
        if missing:
            raise PublicationError(
                "Cannot publish product "
                f"{product_id}: missing translations for {sorted(missing)}"
            )

    async def rebuild_card(self, product_id: int) -> int:
        """Rebuild the ``product_card`` rows for one product (§5.2).

        Deletes existing rows and, if the product is active, writes exactly one
        row per language (ru + ro) from ``product`` + ``product_translation`` +
        main media. Inactive products get no card rows (hidden from listings).

        Args:
            product_id: Product whose read-model to rebuild.

        Returns:
            int: Number of card rows written (0 for inactive products).

        Raises:
            NotFoundError: If the product does not exist.
            PublicationError: If an active product lacks a complete translation set.
        """
        product = await self.repo.get_product(product_id)
        if product is None:
            raise NotFoundError(f"Product {product_id} not found")

        await self.repo.delete_product_cards(product_id)
        if not product.is_active:
            await self.session.flush()
            return 0

        await self.assert_publishable(product_id)
        category = await self.repo.get_category(product.category_id)
        path = category.path if category is not None else [product.category_id]
        main_image_url = await self.repo.get_main_image_url(product_id)
        in_stock = product.qty > 0
        badge = self._compute_badge(product.old_price is not None)
        translations = await self.repo.get_product_translations(product_id)

        written = 0
        for translation in translations:
            card = ProductCard(
                product_id=product.id,
                lang=translation.lang,
                category_id=product.category_id,
                name=translation.name,
                slug=translation.slug,
                price=product.price,
                old_price=product.old_price,
                in_stock=in_stock,
                main_image_url=main_image_url,
                badge=badge,
                path=list(path),
                is_active=product.is_active,
            )
            await self.repo.upsert_card(card)
            written += 1
        await self.session.flush()
        return written

    async def rebuild_cards(self, product_ids: list[int]) -> int:
        """Rebuild the read-model for many products (bulk import path, §5.2).

        Args:
            product_ids: Products to rebuild.

        Returns:
            int: Total number of card rows written across all products.
        """
        total = 0
        for product_id in product_ids:
            total += await self.rebuild_card(product_id)
        return total

    @staticmethod
    def _compute_badge(has_old_price: bool) -> str | None:
        """Derive the listing badge from product state (v1: sale on old_price)."""
        return "sale" if has_old_price else None
