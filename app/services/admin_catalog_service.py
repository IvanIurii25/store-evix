"""Back-office write logic for the catalog (stage B7).

Thin, explicit admin operations over the catalog: category CRUD (+ translations,
reorder, move-with-subtree-path-recompute), product CRUD (+ translations, media
uploads, attribute links, activation) and attribute CRUD (+ values). It reuses
:class:`~app.services.catalog_service.CatalogService` for the publication rule
and the ``product_card`` read-model rebuild — those invariants live in exactly
one place (§10). No HTTP knowledge: the raised errors subclass
:class:`~app.core.errors.DomainError`, which the global handler renders.
"""

import asyncio
import itertools
import logging

from fastapi import UploadFile, status
from sqlalchemy import BigInteger, and_, cast, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY, array
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.images import (
    MAX_UPLOAD_BYTES,
    ImageValidationError,
    validate_and_build_variants,
)
from app.core.storage import get_storage
from app.models.catalog import (
    ALLOWED_LANGS,
    ALLOWED_MEDIA_KINDS,
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
    ProductVariant,
    ProductVariantValue,
    ProductVariationAttribute,
)
from app.schemas.admin_catalog import (
    AttributeCreate,
    AttributeTranslationIn,
    AttributeUpdate,
    AttributeValueCreate,
    AttributeValueTranslationIn,
    AttributeValueUpdate,
    CategoryCreate,
    CategoryReorderItem,
    CategoryTranslationIn,
    CategoryUpdate,
    ProductCreate,
    ProductTranslationIn,
    ProductUpdate,
    VariantCreate,
    VariantUpdate,
)
from app.services.catalog_service import CatalogService

logger = logging.getLogger(__name__)

# Default media kind for v1 uploads (only images are allowed — §2.1).
DEFAULT_MEDIA_KIND: str = "image"

# Stock at or below this is surfaced as "low stock" in the back-office (§4.2).
LOW_STOCK_THRESHOLD: int = 5


class AdminCatalogError(DomainError):
    """Base class for admin-catalog domain errors (rendered by the global handler)."""

    code: str = "admin_catalog_error"


class AdminNotFoundError(AdminCatalogError):
    """A referenced admin resource does not exist."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class AdminConflictError(AdminCatalogError):
    """A write would violate a uniqueness / relational invariant."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class AdminValidationError(AdminCatalogError):
    """A payload is structurally valid but semantically rejected."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "invalid"


class AdminCatalogService:
    """Write operations for the catalog back-office, bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and the shared catalog service.

        Args:
            session: Active async session used for all reads and writes.
        """
        self.session = session
        self.catalog = CatalogService(session)

    # ------------------------------------------------------------------ #
    # Categories
    # ------------------------------------------------------------------ #
    async def _get_category(self, category_id: int) -> Category:
        """Load a category or raise :class:`AdminNotFoundError`.

        Args:
            category_id: Category primary key.

        Returns:
            Category: The category row.

        Raises:
            AdminNotFoundError: If no such category exists.
        """
        category = await self.session.get(Category, category_id)
        if category is None:
            raise AdminNotFoundError(f"Category {category_id} not found")
        return category

    async def _resolve_path(self, parent_id: int | None) -> list[int]:
        """Compute the ancestor prefix (root..parent) for a new/moved node.

        Args:
            parent_id: The parent category id, or ``None`` for a root node.

        Returns:
            list[int]: The parent's ``path`` (empty when the node is a root).

        Raises:
            AdminNotFoundError: If ``parent_id`` refers to a missing category.
        """
        if parent_id is None:
            return []
        parent = await self._get_category(parent_id)
        return list(parent.path)

    async def create_category(self, payload: CategoryCreate) -> Category:
        """Create a category, derive its ``path``/``depth`` and add translations.

        Args:
            payload: New category structure plus initial translations.

        Returns:
            Category: The persisted category.

        Raises:
            AdminNotFoundError: If ``parent_id`` references a missing category.
        """
        prefix = await self._resolve_path(payload.parent_id)
        category = Category(
            parent_id=payload.parent_id,
            path=[],  # placeholder; needs the generated id before it is final
            depth=len(prefix),
            position=payload.position,
            is_active=payload.is_active,
        )
        self.session.add(category)
        await self.session.flush()
        category.path = prefix + [category.id]
        for translation in payload.translations:
            self._add_category_translation(category.id, translation)
        await self.session.commit()
        await self.session.refresh(category)
        return category

    def _add_category_translation(
        self,
        category_id: int,
        payload: CategoryTranslationIn,
    ) -> None:
        """Stage a category translation row for insertion.

        Args:
            category_id: Owning category id.
            payload: Translation content.
        """
        self.session.add(
            CategoryTranslation(
                category_id=category_id,
                lang=payload.lang,
                name=payload.name,
                slug=payload.slug,
                seo_title=payload.seo_title,
                seo_description=payload.seo_description,
            )
        )

    async def update_category(
        self,
        category_id: int,
        payload: CategoryUpdate,
    ) -> Category:
        """Update a category's structural fields (parent change delegates to move).

        Args:
            category_id: Category to update.
            payload: Partial structural update.

        Returns:
            Category: The updated category.

        Raises:
            AdminNotFoundError: If the category (or new parent) does not exist.
        """
        category = await self._get_category(category_id)
        data = payload.model_dump(exclude_unset=True)
        if "parent_id" in data:
            await self.move_category(category_id, data.pop("parent_id"))
            category = await self._get_category(category_id)
        for field, value in data.items():
            setattr(category, field, value)
        await self.session.commit()
        await self.session.refresh(category)
        return category

    async def delete_category(self, category_id: int) -> None:
        """Delete a category and its translations.

        Args:
            category_id: Category to delete.

        Raises:
            AdminNotFoundError: If the category does not exist.
            AdminConflictError: If the category still has children or products.
        """
        category = await self._get_category(category_id)
        if await self._has_children(category_id):
            raise AdminConflictError("Category has child categories")
        if await self._has_products(category_id):
            raise AdminConflictError("Category still has products")
        await self.session.execute(
            delete(CategoryTranslation).where(
                CategoryTranslation.category_id == category_id
            )
        )
        await self.session.delete(category)
        await self.session.commit()

    async def _has_children(self, category_id: int) -> bool:
        """Return whether the category has any direct children."""
        stmt = (
            select(func.count())
            .select_from(Category)
            .where(Category.parent_id == category_id)
        )
        return bool((await self.session.execute(stmt)).scalar_one())

    async def _has_products(self, category_id: int) -> bool:
        """Return whether any product is directly attached to the category."""
        stmt = (
            select(func.count())
            .select_from(Product)
            .where(Product.category_id == category_id)
        )
        return bool((await self.session.execute(stmt)).scalar_one())

    async def set_category_translation(
        self,
        category_id: int,
        payload: CategoryTranslationIn,
    ) -> CategoryTranslation:
        """Upsert one language translation of a category.

        Args:
            category_id: Owning category.
            payload: Translation content (keyed by ``lang``).

        Returns:
            CategoryTranslation: The upserted translation.

        Raises:
            AdminNotFoundError: If the category does not exist.
        """
        await self._get_category(category_id)
        stmt = select(CategoryTranslation).where(
            CategoryTranslation.category_id == category_id,
            CategoryTranslation.lang == payload.lang,
        )
        translation = (await self.session.execute(stmt)).scalar_one_or_none()
        if translation is None:
            translation = CategoryTranslation(
                category_id=category_id,
                lang=payload.lang,
            )
            self.session.add(translation)
        translation.name = payload.name
        translation.slug = payload.slug
        translation.seo_title = payload.seo_title
        translation.seo_description = payload.seo_description
        await self.session.commit()
        await self.session.refresh(translation)
        return translation

    async def reorder_categories(self, items: list[CategoryReorderItem]) -> None:
        """Assign new ``position`` values to a set of categories.

        Args:
            items: ``(category_id, position)`` assignments.

        Raises:
            AdminNotFoundError: If any referenced category is missing.
        """
        for item in items:
            category = await self._get_category(item.category_id)
            category.position = item.position
        await self.session.commit()

    async def move_category(
        self,
        category_id: int,
        new_parent_id: int | None,
    ) -> Category:
        """Re-parent a category and recompute ``path``/``depth`` for the subtree.

        The node's new ``path`` is ``parent.path + [node_id]``; every descendant
        (matched via ``path @> ARRAY[node_id]``) has its old ancestor-prefix
        replaced with the node's new path, preserving its relative sub-path.

        Args:
            category_id: The category being moved.
            new_parent_id: The new parent id, or ``None`` to move to root.

        Returns:
            Category: The moved category with a fresh ``path``/``depth``.

        Raises:
            AdminNotFoundError: If the node or the new parent does not exist.
            AdminValidationError: If the move would create a cycle (parent is a
                descendant of the node).
        """
        node = await self._get_category(category_id)
        prefix = await self._resolve_path(new_parent_id)
        if category_id in prefix:
            raise AdminValidationError("Cannot move a category under its own subtree")

        old_path = list(node.path)
        new_node_path = prefix + [category_id]

        # Fetch the whole subtree (node + all descendants) in one query.
        # ``path`` is ``bigint[]`` — cast the literal so the ``@>`` operand types
        # match (asyncpg has no ``bigint[] @> integer[]``).
        subtree_stmt = select(Category).where(
            Category.path.contains(cast(array([category_id]), ARRAY(BigInteger)))
        )
        subtree = (await self.session.execute(subtree_stmt)).scalars().all()

        old_len = len(old_path)
        for member in subtree:
            # Replace the ``old_path`` prefix with ``new_node_path``; the tail
            # (member's path below the moved node) is preserved verbatim.
            tail = list(member.path)[old_len:]
            member.path = new_node_path + tail
            member.depth = len(member.path) - 1
            if member.id == category_id:
                member.parent_id = new_parent_id

        await self.session.commit()
        await self.session.refresh(node)
        return node

    async def get_category_translations(
        self,
        category_id: int,
    ) -> list[CategoryTranslation]:
        """Return all translations of a category (for the admin response)."""
        stmt = select(CategoryTranslation).where(
            CategoryTranslation.category_id == category_id
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_categories(self) -> list[Category]:
        """Return every category (active or not) ordered for tree assembly.

        Ordered by ``path`` then ``position`` so a flat client can rebuild the
        tree deterministically. Unlike the storefront catalog tree this includes
        inactive categories — the back office manages them.
        """
        stmt = select(Category).order_by(Category.path, Category.position)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_category_out_data(
        self,
        categories: list[Category],
    ) -> dict[int, list[CategoryTranslation]]:
        """Return ``{category_id: translations}`` for a set of categories.

        One batched query (no per-category N+1) so the flat list endpoint can
        attach translations to every node.
        """
        if not categories:
            return {}
        ids = [category.id for category in categories]
        stmt = select(CategoryTranslation).where(
            CategoryTranslation.category_id.in_(ids)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        grouped: dict[int, list[CategoryTranslation]] = {cid: [] for cid in ids}
        for row in rows:
            grouped[row.category_id].append(row)
        return grouped

    # ------------------------------------------------------------------ #
    # Products
    # ------------------------------------------------------------------ #
    async def _get_product(self, product_id: int) -> Product:
        """Load a product or raise :class:`AdminNotFoundError`."""
        product = await self.session.get(Product, product_id)
        if product is None:
            raise AdminNotFoundError(f"Product {product_id} not found")
        return product

    async def create_product(self, payload: ProductCreate) -> Product:
        """Create a product with translations; rebuild the card if active.

        Args:
            payload: New product structure plus initial translations.

        Returns:
            Product: The persisted product.

        Raises:
            AdminNotFoundError: If ``category_id`` is missing.
            PublicationError: If created active without both translations (§2.1.1).
        """
        await self._get_category(payload.category_id)
        product = Product(
            category_id=payload.category_id,
            code=payload.code,
            price=payload.price,
            old_price=payload.old_price,
            qty=payload.qty,
            weight_g=payload.weight_g,
            is_active=payload.is_active,
            is_featured=payload.is_featured,
            has_variants=payload.has_variants,
        )
        self.session.add(product)
        await self.session.flush()
        for translation in payload.translations:
            self._add_product_translation(product.id, translation)
        await self.session.flush()
        if product.is_active:
            await self.catalog.assert_publishable(product.id)
            await self.catalog.rebuild_cards([product.id])
        await self.session.commit()
        await self.session.refresh(product)
        self._enqueue_es_reindex([product.id])
        return product

    def _add_product_translation(
        self,
        product_id: int,
        payload: ProductTranslationIn,
    ) -> None:
        """Stage a product translation row for insertion."""
        self.session.add(
            ProductTranslation(
                product_id=product_id,
                lang=payload.lang,
                name=payload.name,
                slug=payload.slug,
                description=payload.description,
                seo_title=payload.seo_title,
                seo_description=payload.seo_description,
            )
        )

    async def update_product(
        self,
        product_id: int,
        payload: ProductUpdate,
    ) -> Product:
        """Update product fields; enforce publication + rebuild the card.

        Activation (``is_active`` going true) requires translations in both
        languages via :meth:`CatalogService.assert_publishable`; afterwards the
        ``product_card`` read-model is rebuilt so listings stay consistent.

        Args:
            product_id: Product to update.
            payload: Partial structural / price / stock update.

        Returns:
            Product: The updated product.

        Raises:
            AdminNotFoundError: If the product (or new category) is missing.
            PublicationError: On activation without both translations (§2.1.1).
        """
        product = await self._get_product(product_id)
        # Capture stock BEFORE applying so we can detect an out-of-stock →
        # in-stock transition and fire the restock-notification task (§3).
        old_qty = product.qty
        data = payload.model_dump(exclude_unset=True)
        if "category_id" in data and data["category_id"] is not None:
            await self._get_category(data["category_id"])
        for field, value in data.items():
            setattr(product, field, value)
        await self.session.flush()
        if product.is_active:
            await self.catalog.assert_publishable(product.id)
        await self.catalog.rebuild_cards([product.id])
        await self.session.commit()
        await self.session.refresh(product)
        # Post-commit side effect: on a 0 → in-stock transition, enqueue the
        # restock-notification task. Enqueue is OUTSIDE the transaction and
        # defensively wrapped — a broker hiccup must never fail a committed
        # product update.
        self._notify_if_restocked(product.id, old_qty, product.qty)
        self._enqueue_es_reindex([product.id])
        return product

    @staticmethod
    def _notify_if_restocked(product_id: int, old_qty: int, new_qty: int) -> None:
        """Enqueue the restock task if stock went from ``<=0`` to ``>0`` (§3).

        The Celery task is imported lazily to avoid an import cycle
        (``app.tasks.restock`` → service). Any enqueue failure is swallowed and
        logged so a broker outage cannot fail an already-committed product write.

        Args:
            product_id: The updated product.
            old_qty: Stock before the update.
            new_qty: Stock after the update.
        """
        if not (old_qty <= 0 and new_qty > 0):
            return
        try:
            from app.tasks.restock import send_restock_notifications

            send_restock_notifications.delay(product_id)
        except Exception:  # noqa: BLE001 — enqueue must not fail the product write
            logger.exception(
                "restock: failed to enqueue notification for product %s", product_id
            )

    @staticmethod
    def _enqueue_es_reindex(product_ids: list[int]) -> None:
        """Enqueue an ES re-index of changed products (search freshness hook).

        Post-commit, lazily imported and swallowed — mirrors ``_notify_if_restocked``.
        The task itself no-ops under the Postgres backend, so this is safe to call
        unconditionally; a broker outage must never fail a committed product write.
        """
        try:
            from app.tasks.es_sync import index_products

            index_products.delay(product_ids)
        except Exception:  # noqa: BLE001 — enqueue must not fail the product write
            logger.exception("es: failed to enqueue reindex for %s", product_ids)

    @staticmethod
    def _enqueue_es_delete(product_ids: list[int]) -> None:
        """Enqueue removal of deleted products from the ES index (post-commit)."""
        try:
            from app.tasks.es_sync import delete_products

            delete_products.delay(product_ids)
        except Exception:  # noqa: BLE001 — enqueue must not fail the product write
            logger.exception("es: failed to enqueue delete for %s", product_ids)

    async def delete_product(self, product_id: int) -> None:
        """Delete a product with its translations, media, attributes and cards.

        Args:
            product_id: Product to delete.

        Raises:
            AdminNotFoundError: If the product does not exist.
        """
        product = await self._get_product(product_id)
        await self.session.execute(
            delete(ProductTranslation).where(
                ProductTranslation.product_id == product_id
            )
        )
        await self.session.execute(delete(Media).where(Media.product_id == product_id))
        await self.session.execute(
            delete(ProductAttribute).where(ProductAttribute.product_id == product_id)
        )
        # Variant cleanup (order matters — FKs have no cascade): values → variants
        # → variation attributes. Media (incl. variant-bound) was deleted above;
        # order_item.variant_id is SET NULL so order history survives.
        variant_ids = list(
            (
                await self.session.execute(
                    select(ProductVariant.id).where(
                        ProductVariant.product_id == product_id
                    )
                )
            )
            .scalars()
            .all()
        )
        if variant_ids:
            await self.session.execute(
                delete(ProductVariantValue).where(
                    ProductVariantValue.variant_id.in_(variant_ids)
                )
            )
        await self.session.execute(
            delete(ProductVariationAttribute).where(
                ProductVariationAttribute.product_id == product_id
            )
        )
        await self.session.execute(
            delete(ProductVariant).where(ProductVariant.product_id == product_id)
        )
        # Drop the read-model rows (product may have been active).
        await self.catalog.repo.delete_product_cards(product_id)
        await self.session.delete(product)
        await self.session.commit()
        self._enqueue_es_delete([product_id])

    async def set_product_translation(
        self,
        product_id: int,
        payload: ProductTranslationIn,
    ) -> ProductTranslation:
        """Upsert one language translation of a product and rebuild the card.

        Args:
            product_id: Owning product.
            payload: Translation content (keyed by ``lang``).

        Returns:
            ProductTranslation: The upserted translation.

        Raises:
            AdminNotFoundError: If the product does not exist.
            PublicationError: If the product is active but ends up incomplete.
        """
        product = await self._get_product(product_id)
        stmt = select(ProductTranslation).where(
            ProductTranslation.product_id == product_id,
            ProductTranslation.lang == payload.lang,
        )
        translation = (await self.session.execute(stmt)).scalar_one_or_none()
        if translation is None:
            translation = ProductTranslation(
                product_id=product_id,
                lang=payload.lang,
            )
            self.session.add(translation)
        translation.name = payload.name
        translation.slug = payload.slug
        translation.description = payload.description
        translation.seo_title = payload.seo_title
        translation.seo_description = payload.seo_description
        await self.session.flush()
        if product.is_active:
            await self.catalog.assert_publishable(product_id)
        await self.catalog.rebuild_cards([product_id])
        await self.session.commit()
        await self.session.refresh(translation)
        self._enqueue_es_reindex([product_id])
        return translation

    async def set_product_attributes(
        self,
        product_id: int,
        value_ids: list[int],
    ) -> list[int]:
        """Replace a product's attribute-value links with the given set.

        Args:
            product_id: Owning product.
            value_ids: Attribute-value ids to link (duplicates ignored).

        Returns:
            list[int]: The linked value ids after the write.

        Raises:
            AdminNotFoundError: If the product or any value id is missing.
        """
        await self._get_product(product_id)
        unique_ids = list(dict.fromkeys(value_ids))
        if unique_ids:
            found = (
                (
                    await self.session.execute(
                        select(AttributeValue.id).where(
                            AttributeValue.id.in_(unique_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            missing = set(unique_ids) - set(found)
            if missing:
                raise AdminNotFoundError(
                    f"Attribute values not found: {sorted(missing)}"
                )
        await self.session.execute(
            delete(ProductAttribute).where(ProductAttribute.product_id == product_id)
        )
        for value_id in unique_ids:
            self.session.add(ProductAttribute(product_id=product_id, value_id=value_id))
        await self.session.commit()
        self._enqueue_es_reindex([product_id])
        return unique_ids

    # ------------------------------------------------------------------ #
    # Variants (variable products)
    # ------------------------------------------------------------------ #
    async def get_product_variation_attribute_ids(self, product_id: int) -> list[int]:
        """Return a product's variation-selector attribute ids, in selector order."""
        stmt = (
            select(ProductVariationAttribute.attribute_id)
            .where(ProductVariationAttribute.product_id == product_id)
            .order_by(ProductVariationAttribute.position)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_product_variants(self, product_id: int) -> list[ProductVariant]:
        """Return ALL of a product's variants (incl. inactive), in display order."""
        stmt = (
            select(ProductVariant)
            .where(ProductVariant.product_id == product_id)
            .order_by(ProductVariant.position, ProductVariant.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_variant_value_ids_map(
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
        out: dict[int, list[int]] = {}
        for variant_id, value_id in (await self.session.execute(stmt)).all():
            out.setdefault(variant_id, []).append(value_id)
        return out

    async def get_variant_image_ids(self, variant_ids: list[int]) -> dict[int, int]:
        """Return ``{variant_id: media_id}`` — the lowest-position bound image."""
        if not variant_ids:
            return {}
        stmt = (
            select(Media.variant_id, Media.id)
            .where(Media.variant_id.in_(variant_ids))
            .order_by(Media.variant_id, Media.position, Media.id)
        )
        out: dict[int, int] = {}
        for variant_id, media_id in (await self.session.execute(stmt)).all():
            if variant_id not in out:  # first seen = lowest position
                out[variant_id] = media_id
        return out

    async def set_variation_attributes(
        self, product_id: int, attribute_ids: list[int]
    ) -> list[int]:
        """Replace a product's variation-selector attributes (ordered).

        A non-empty list marks the product variable; an empty list clears it and
        turns ``has_variants`` off — allowed only when no variants remain.

        Args:
            product_id: Owning product.
            attribute_ids: Ordered attribute ids (duplicates ignored).

        Returns:
            list[int]: The stored attribute ids after the write.

        Raises:
            AdminNotFoundError: If the product or any attribute id is missing.
            AdminValidationError: If clearing while variants still exist.
        """
        product = await self._get_product(product_id)
        unique = list(dict.fromkeys(attribute_ids))
        if unique:
            found = (
                (
                    await self.session.execute(
                        select(Attribute.id).where(Attribute.id.in_(unique))
                    )
                )
                .scalars()
                .all()
            )
            missing = set(unique) - set(found)
            if missing:
                raise AdminNotFoundError(f"Attributes not found: {sorted(missing)}")
        else:
            count = await self.session.scalar(
                select(func.count())
                .select_from(ProductVariant)
                .where(ProductVariant.product_id == product_id)
            )
            if count:
                raise AdminValidationError(
                    "Delete variants before clearing variation attributes"
                )
        await self.session.execute(
            delete(ProductVariationAttribute).where(
                ProductVariationAttribute.product_id == product_id
            )
        )
        for position, attribute_id in enumerate(unique):
            self.session.add(
                ProductVariationAttribute(
                    product_id=product_id,
                    attribute_id=attribute_id,
                    position=position,
                )
            )
        product.has_variants = bool(unique)
        await self.session.flush()
        await self.catalog.rebuild_cards([product_id])
        await self.session.commit()
        self._enqueue_es_reindex([product_id])
        return unique

    async def create_variant(
        self, product_id: int, payload: VariantCreate
    ) -> ProductVariant:
        """Create one variant — a full combination of the variation attributes.

        Args:
            product_id: Owning product.
            payload: Value ids (one per variation attribute) + price/stock/code.

        Returns:
            ProductVariant: The created variant.

        Raises:
            AdminNotFoundError: If the product or a value id is missing.
            AdminValidationError: If no variation attributes are set, or the value
                set is not exactly one value per variation attribute.
            AdminConflictError: If the code, or the exact combination, already exists.
        """
        product = await self._get_product(product_id)
        variation_attr_ids = await self.get_product_variation_attribute_ids(product_id)
        if not variation_attr_ids:
            raise AdminValidationError(
                "Set variation attributes before adding variants"
            )
        unique_values = list(dict.fromkeys(payload.value_ids))
        rows = (
            await self.session.execute(
                select(AttributeValue.id, AttributeValue.attribute_id).where(
                    AttributeValue.id.in_(unique_values)
                )
            )
        ).all()
        found = {vid: aid for vid, aid in rows}
        missing = set(unique_values) - set(found)
        if missing:
            raise AdminNotFoundError(f"Attribute values not found: {sorted(missing)}")
        attr_ids = [found[v] for v in unique_values]
        if sorted(attr_ids) != sorted(variation_attr_ids):
            raise AdminValidationError(
                "Provide exactly one value per variation attribute"
            )
        await self._assert_variant_code_free(payload.code)
        if await self._combo_exists(product_id, set(unique_values)):
            raise AdminConflictError("A variant with this combination already exists")

        position = await self.session.scalar(
            select(func.coalesce(func.max(ProductVariant.position), -1) + 1).where(
                ProductVariant.product_id == product_id
            )
        )
        variant = ProductVariant(
            product_id=product_id,
            code=payload.code,
            price=payload.price,
            old_price=payload.old_price,
            qty=payload.qty,
            weight_g=payload.weight_g,
            is_active=payload.is_active,
            position=position or 0,
        )
        self.session.add(variant)
        await self.session.flush()
        for value_id in unique_values:
            self.session.add(
                ProductVariantValue(variant_id=variant.id, value_id=value_id)
            )
        if not product.has_variants:
            product.has_variants = True
        await self.session.flush()
        await self.catalog.rebuild_cards([product_id])
        await self.session.commit()
        self._enqueue_es_reindex([product_id])
        await self.session.refresh(variant)
        return variant

    async def generate_variants(
        self, product_id: int
    ) -> tuple[list[ProductVariant], int]:
        """Create the missing cartesian combinations of the variation attributes.

        Combines the product's **assigned** attribute values (``product_attribute``)
        for each variation attribute; existing combinations are left intact and only
        the missing ones are created (at the product's price, qty 0, active). The
        operation is idempotent — re-running it only fills gaps.

        Args:
            product_id: Owning product.

        Returns:
            tuple[list[ProductVariant], int]: The newly created variants (possibly
            empty) and the number of combinations skipped because a matching variant
            already existed.

        Raises:
            AdminNotFoundError: If the product does not exist.
            AdminValidationError: If no variation attributes are set, or a variation
                attribute has no assigned values to combine.
        """
        product = await self._get_product(product_id)
        variation_attr_ids = await self.get_product_variation_attribute_ids(product_id)
        if not variation_attr_ids:
            raise AdminValidationError(
                "Set variation attributes before generating variants"
            )
        rows = (
            await self.session.execute(
                select(AttributeValue.attribute_id, AttributeValue.id)
                .join(
                    ProductAttribute,
                    ProductAttribute.value_id == AttributeValue.id,
                )
                .where(
                    ProductAttribute.product_id == product_id,
                    AttributeValue.attribute_id.in_(variation_attr_ids),
                )
                .order_by(AttributeValue.attribute_id, AttributeValue.id)
            )
        ).all()
        by_attr: dict[int, list[int]] = {}
        for attribute_id, value_id in rows:
            by_attr.setdefault(attribute_id, []).append(value_id)
        missing = [a for a in variation_attr_ids if a not in by_attr]
        if missing:
            raise AdminValidationError(
                "Assign values for every variation attribute before generating"
            )

        existing = await self.get_product_variants(product_id)
        existing_map = await self.get_variant_value_ids_map([v.id for v in existing])
        existing_combos = {frozenset(vals) for vals in existing_map.values()}
        position = (
            await self.session.scalar(
                select(func.coalesce(func.max(ProductVariant.position), -1) + 1).where(
                    ProductVariant.product_id == product_id
                )
            )
            or 0
        )

        ordered_values = [by_attr[a] for a in variation_attr_ids]
        created: list[ProductVariant] = []
        skipped = 0
        for combo in itertools.product(*ordered_values):
            combo_set = frozenset(combo)
            if combo_set in existing_combos:
                skipped += 1
                continue
            variant = ProductVariant(
                product_id=product_id,
                code=None,
                price=product.price,
                old_price=None,
                qty=0,
                is_active=True,
                position=position,
            )
            self.session.add(variant)
            await self.session.flush()
            for value_id in combo:
                self.session.add(
                    ProductVariantValue(variant_id=variant.id, value_id=value_id)
                )
            created.append(variant)
            existing_combos.add(combo_set)
            position += 1

        if created and not product.has_variants:
            product.has_variants = True
        await self.session.flush()
        await self.catalog.rebuild_cards([product_id])
        await self.session.commit()
        self._enqueue_es_reindex([product_id])
        for variant in created:
            await self.session.refresh(variant)
        return created, skipped

    async def update_variant(
        self, variant_id: int, payload: VariantUpdate
    ) -> ProductVariant:
        """Partially update a variant; rebuild its product's card.

        Args:
            variant_id: Variant to update.
            payload: Fields to change (unset fields are left as-is).

        Returns:
            ProductVariant: The updated variant.

        Raises:
            AdminNotFoundError: If the variant does not exist.
            AdminConflictError: If a new ``code`` collides with another variant.
        """
        variant = await self._get_variant(variant_id)
        data = payload.model_dump(exclude_unset=True)
        if "code" in data and data["code"] and data["code"] != variant.code:
            await self._assert_variant_code_free(data["code"])
        for field, value in data.items():
            setattr(variant, field, value)
        await self.session.flush()
        # Guard: don't leave an ACTIVE product with no active variants (e.g. by
        # deactivating its last one). PublicationError rolls the change back.
        product = await self._get_product(variant.product_id)
        if product.is_active:
            await self.catalog.assert_publishable(variant.product_id)
        await self.catalog.rebuild_cards([variant.product_id])
        await self.session.commit()
        self._enqueue_es_reindex([variant.product_id])
        await self.session.refresh(variant)
        return variant

    async def delete_variant(self, variant_id: int) -> None:
        """Delete a variant (its value links + image bindings); rebuild the card.

        Args:
            variant_id: Variant to remove.

        Raises:
            AdminNotFoundError: If the variant does not exist.
        """
        variant = await self._get_variant(variant_id)
        product_id = variant.product_id
        await self.session.execute(
            update(Media).where(Media.variant_id == variant_id).values(variant_id=None)
        )
        await self.session.execute(
            delete(ProductVariantValue).where(
                ProductVariantValue.variant_id == variant_id
            )
        )
        await self.session.execute(
            delete(ProductVariant).where(ProductVariant.id == variant_id)
        )
        await self.session.flush()
        # Guard: deleting the last active variant of an ACTIVE product would leave
        # it published but unbuyable. PublicationError rolls the delete back.
        product = await self._get_product(product_id)
        if product.is_active:
            await self.catalog.assert_publishable(product_id)
        await self.catalog.rebuild_cards([product_id])
        await self.session.commit()
        self._enqueue_es_reindex([product_id])

    async def bind_media_to_variant(
        self, product_id: int, media_id: int, variant_id: int | None
    ) -> Media:
        """Bind (or unbind, when ``variant_id`` is None) a media row to a variant.

        Args:
            product_id: Owning product (both media and variant must belong to it).
            media_id: The media row to (re)bind.
            variant_id: Target variant id, or ``None`` to move it to the shared
                gallery.

        Returns:
            Media: The updated media row.

        Raises:
            AdminNotFoundError: If the media or variant is missing or not on the
                product.
        """
        await self._get_product(product_id)
        media = await self.session.get(Media, media_id)
        if media is None or media.product_id != product_id:
            raise AdminNotFoundError(f"Media {media_id} not found")
        if variant_id is not None:
            variant = await self.session.get(ProductVariant, variant_id)
            if variant is None or variant.product_id != product_id:
                raise AdminNotFoundError(f"Variant {variant_id} not found")
        media.variant_id = variant_id
        await self.session.flush()
        await self.catalog.rebuild_cards([product_id])
        await self.session.commit()
        self._enqueue_es_reindex([product_id])
        await self.session.refresh(media)
        return media

    async def _get_variant(self, variant_id: int) -> ProductVariant:
        """Load a variant or raise :class:`AdminNotFoundError`."""
        variant = await self.session.get(ProductVariant, variant_id)
        if variant is None:
            raise AdminNotFoundError(f"Variant {variant_id} not found")
        return variant

    async def _assert_variant_code_free(self, code: str | None) -> None:
        """Raise :class:`AdminConflictError` if ``code`` is already taken."""
        if not code:
            return
        exists = await self.session.scalar(
            select(ProductVariant.id).where(ProductVariant.code == code)
        )
        if exists is not None:
            raise AdminConflictError(f"Variant code '{code}' already exists")

    async def _combo_exists(self, product_id: int, value_ids: set[int]) -> bool:
        """Return whether a variant with the exact value set already exists."""
        existing = await self.get_product_variants(product_id)
        if not existing:
            return False
        value_map = await self.get_variant_value_ids_map([v.id for v in existing])
        return any(set(vals) == value_ids for vals in value_map.values())

    async def add_product_media(
        self,
        product_id: int,
        upload: UploadFile,
    ) -> Media:
        """Store an uploaded image and record a :class:`Media` row.

        The bytes are read from the upload, validated + converted into the fixed
        responsive WebP set (the upload is the optimization gate — non-raster or
        oversized files are rejected), stored via the configured backend
        (:func:`~app.core.storage.get_storage` — local dir or S3/MinIO), and the
        variants are persisted alongside the original. The original's public URL
        is stored as the DB ``url``.

        Args:
            product_id: Product the image belongs to.
            upload: The uploaded file.

        Returns:
            Media: The persisted media row.

        Raises:
            AdminNotFoundError: If the product does not exist.
            AdminValidationError: If the file is not an allowed image kind,
                exceeds the max upload byte size, or is not a decodable raster /
                exceeds the max upload dimension.
        """
        await self._get_product(product_id)
        if not self._is_allowed_image(upload):
            raise AdminValidationError("Only image uploads are allowed")
        if upload.size is not None and upload.size > MAX_UPLOAD_BYTES:
            raise AdminValidationError("Image exceeds the maximum upload size")

        await upload.seek(0)
        data = await upload.read()
        try:
            variants = await asyncio.to_thread(validate_and_build_variants, data)
        except ImageValidationError as exc:
            raise AdminValidationError("Unsupported or too-large image") from exc
        storage = get_storage()
        url = await storage.save(
            data,
            filename=upload.filename or "",
            content_type=upload.content_type or "application/octet-stream",
        )
        await storage.save_variants(url, variants)
        position = await self._next_media_position(product_id)
        media = Media(
            product_id=product_id,
            url=url,
            kind=DEFAULT_MEDIA_KIND,
            position=position,
        )
        self.session.add(media)
        await self.session.flush()
        # Refresh the card: the main image may have changed.
        product = await self._get_product(product_id)
        if product.is_active:
            await self.catalog.rebuild_cards([product_id])
        await self.session.commit()
        await self.session.refresh(media)
        return media

    async def delete_product_media(self, product_id: int, media_id: int) -> None:
        """Remove one image from a product (DB row + best-effort stored object).

        The stored object is deleted after the row so a storage hiccup never
        leaves a dangling DB row; a missing object is a no-op. The card is
        rebuilt when the product is active (the main image may have changed).

        Args:
            product_id: Owning product.
            media_id: The media row to delete.

        Raises:
            AdminNotFoundError: If the product or the media row does not exist
                (the media must belong to the given product).
        """
        product = await self._get_product(product_id)
        media = await self.session.get(Media, media_id)
        if media is None or media.product_id != product_id:
            raise AdminNotFoundError(f"Media not found: {media_id}")
        url = media.url
        await self.session.delete(media)
        await self.session.flush()
        if product.is_active:
            await self.catalog.rebuild_cards([product_id])
        await self.session.commit()
        # Best-effort object removal — never blocks the committed row deletion.
        try:
            await get_storage().delete(url)
        except Exception:  # noqa: BLE001 — storage cleanup must not fail the op
            pass

    async def reorder_product_media(
        self,
        product_id: int,
        ordered_ids: list[int],
    ) -> list[Media]:
        """Set media ``position`` from the given order and return the new list.

        ``ordered_ids`` must be exactly the product's current media ids (a full
        permutation) — this keeps the reorder unambiguous and rejects stale
        clients. The first id becomes the main image, so an active product's
        card is rebuilt.

        Args:
            product_id: Owning product.
            ordered_ids: The media ids in their desired display order.

        Returns:
            list[Media]: The product's media in the new order.

        Raises:
            AdminNotFoundError: If the product does not exist.
            AdminValidationError: If ``ordered_ids`` is not a permutation of the
                product's current media ids.
        """
        product = await self._get_product(product_id)
        current = await self.get_product_media(product_id)
        if set(ordered_ids) != {media.id for media in current} or len(
            ordered_ids
        ) != len(current):
            raise AdminValidationError(
                "ordered_ids must be a permutation of the product's media ids"
            )
        by_id = {media.id: media for media in current}
        for position, media_id in enumerate(ordered_ids):
            by_id[media_id].position = position
        await self.session.flush()
        if product.is_active:
            await self.catalog.rebuild_cards([product_id])
        await self.session.commit()
        return await self.get_product_media(product_id)

    @staticmethod
    def _is_allowed_image(upload: UploadFile) -> bool:
        """Return whether the upload declares an allowed image content type."""
        content_type = (upload.content_type or "").lower()
        if not content_type.startswith("image/"):
            return False
        return DEFAULT_MEDIA_KIND in ALLOWED_MEDIA_KINDS

    async def _next_media_position(self, product_id: int) -> int:
        """Return the next free ``position`` for a product's media (append)."""
        stmt = select(func.coalesce(func.max(Media.position), -1)).where(
            Media.product_id == product_id
        )
        return int((await self.session.execute(stmt)).scalar_one()) + 1

    async def search_products(
        self,
        search: str | None,
        limit: int = 50,
        *,
        is_active: bool | None = None,
        low_stock: bool = False,
        on_sale: bool = False,
        no_weight: bool = False,
    ) -> list[tuple[Product, str | None]]:
        """Search products for the back-office by ``code`` or translated name.

        Args:
            search: Free-text term matched (case-insensitively) against the
                product ``code`` and any translation ``name``. ``None``/empty
                returns the most recent products.
            limit: Maximum number of rows to return.
            is_active: Restrict to active (``True``) / inactive (``False``)
                products, or ``None`` for both.
            low_stock: When ``True``, only products with ``qty`` at or below
                :data:`LOW_STOCK_THRESHOLD` (the back-office restock queue).
            on_sale: When ``True``, only products on sale — ``old_price`` set and
                strictly greater than ``price`` (the "Акции" list, §4.2).
            no_weight: When ``True``, only products with no shipping weight
                entered (``weight_g IS NULL``) — the queue of what still has to
                be filled in before a carrier can price it honestly.

        Returns:
            list[tuple[Product, str | None]]: ``(product, display_name)`` rows,
            where ``display_name`` is the default-language (``ALLOWED_LANGS[0]``)
            translation name — one row per product.
        """
        # Step 1 — select the DISTINCT product ids to return. The outer-join to
        # ProductTranslation keeps name-search matching across *either* language,
        # while ``.distinct()`` before ``.limit(limit)`` makes the cap bound the
        # number of distinct products (not the joined rows) — otherwise a two-row
        # (ru + ro) join would halve the page (§4.2 back-office list).
        id_stmt = (
            select(Product.id)
            .outerjoin(
                ProductTranslation,
                ProductTranslation.product_id == Product.id,
            )
            .distinct()
            .order_by(Product.id.desc())
            .limit(limit)
        )
        if search:
            term = f"%{search.strip()}%"
            id_stmt = id_stmt.where(
                or_(
                    Product.code.ilike(term),
                    ProductTranslation.name.ilike(term),
                )
            )
        if is_active is not None:
            id_stmt = id_stmt.where(Product.is_active.is_(is_active))
        if low_stock:
            id_stmt = id_stmt.where(Product.qty <= LOW_STOCK_THRESHOLD)
        if on_sale:
            id_stmt = id_stmt.where(
                Product.old_price.is_not(None),
                Product.old_price > Product.price,
            )
        if no_weight:
            id_stmt = id_stmt.where(Product.weight_g.is_(None))
        product_ids = list((await self.session.execute(id_stmt)).scalars().all())
        if not product_ids:
            return []

        # Step 2 — load those products for display, joined to the default-language
        # translation name only, so each product yields exactly one row.
        display_lang = ALLOWED_LANGS[0]
        rows_stmt = (
            select(Product, ProductTranslation.name)
            .outerjoin(
                ProductTranslation,
                and_(
                    ProductTranslation.product_id == Product.id,
                    ProductTranslation.lang == display_lang,
                ),
            )
            .where(Product.id.in_(product_ids))
            .order_by(Product.id.desc())
        )
        rows = (await self.session.execute(rows_stmt)).all()
        return [(product, name) for product, name in rows]

    async def get_product_translations(
        self,
        product_id: int,
    ) -> list[ProductTranslation]:
        """Return all translations of a product (for the admin response)."""
        stmt = select(ProductTranslation).where(
            ProductTranslation.product_id == product_id
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_product_media(self, product_id: int) -> list[Media]:
        """Return a product's media ordered by ``position`` (for the response)."""
        stmt = (
            select(Media)
            .where(Media.product_id == product_id)
            .order_by(Media.position, Media.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_product_value_ids(self, product_id: int) -> list[int]:
        """Return the attribute-value ids linked to a product."""
        stmt = select(ProductAttribute.value_id).where(
            ProductAttribute.product_id == product_id
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # ------------------------------------------------------------------ #
    # Attributes + values
    # ------------------------------------------------------------------ #
    async def _get_attribute(self, attribute_id: int) -> Attribute:
        """Load an attribute or raise :class:`AdminNotFoundError`."""
        attribute = await self.session.get(Attribute, attribute_id)
        if attribute is None:
            raise AdminNotFoundError(f"Attribute {attribute_id} not found")
        return attribute

    async def list_attributes(self) -> list[Attribute]:
        """Return every attribute dictionary entry ordered by ``id``.

        The full admin view (translations + values) is assembled per attribute by
        the router via :meth:`get_attribute_detail`, which issues its own explicit
        queries — so this method only needs the attribute rows and never triggers
        an async lazy-load on the relations.

        Returns:
            list[Attribute]: All attributes, ordered by primary key.
        """
        stmt = select(Attribute).order_by(Attribute.id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def create_attribute(self, payload: AttributeCreate) -> Attribute:
        """Create an attribute dictionary entry with its translations.

        Args:
            payload: Attribute code plus per-language display names.

        Returns:
            Attribute: The persisted attribute.
        """
        attribute = Attribute(code=payload.code)
        self.session.add(attribute)
        await self.session.flush()
        for translation in payload.translations:
            self._add_attribute_translation(attribute.id, translation)
        await self.session.commit()
        await self.session.refresh(attribute)
        return attribute

    def _add_attribute_translation(
        self,
        attribute_id: int,
        payload: AttributeTranslationIn,
    ) -> None:
        """Stage an attribute translation row for insertion."""
        self.session.add(
            AttributeTranslation(
                attribute_id=attribute_id,
                lang=payload.lang,
                name=payload.name,
            )
        )

    async def update_attribute(
        self,
        attribute_id: int,
        payload: AttributeUpdate,
    ) -> Attribute:
        """Update an attribute's ``code`` and/or replace its translations.

        Args:
            attribute_id: Attribute to update.
            payload: Partial update.

        Returns:
            Attribute: The updated attribute.

        Raises:
            AdminNotFoundError: If the attribute does not exist.
        """
        attribute = await self._get_attribute(attribute_id)
        if payload.code is not None:
            attribute.code = payload.code
        if payload.translations is not None:
            await self.session.execute(
                delete(AttributeTranslation).where(
                    AttributeTranslation.attribute_id == attribute_id
                )
            )
            for translation in payload.translations:
                self._add_attribute_translation(attribute_id, translation)
        await self.session.commit()
        await self.session.refresh(attribute)
        return attribute

    async def delete_attribute(self, attribute_id: int) -> None:
        """Delete an attribute with its translations, values and value links.

        Args:
            attribute_id: Attribute to delete.

        Raises:
            AdminNotFoundError: If the attribute does not exist.
        """
        attribute = await self._get_attribute(attribute_id)
        value_ids = (
            (
                await self.session.execute(
                    select(AttributeValue.id).where(
                        AttributeValue.attribute_id == attribute_id
                    )
                )
            )
            .scalars()
            .all()
        )
        if value_ids:
            await self.session.execute(
                delete(ProductAttribute).where(ProductAttribute.value_id.in_(value_ids))
            )
            await self.session.execute(
                delete(AttributeValueTranslation).where(
                    AttributeValueTranslation.value_id.in_(value_ids)
                )
            )
            await self.session.execute(
                delete(AttributeValue).where(
                    AttributeValue.attribute_id == attribute_id
                )
            )
        await self.session.execute(
            delete(AttributeTranslation).where(
                AttributeTranslation.attribute_id == attribute_id
            )
        )
        await self.session.delete(attribute)
        await self.session.commit()

    async def create_attribute_value(
        self,
        attribute_id: int,
        payload: AttributeValueCreate,
    ) -> AttributeValue:
        """Create a value under an attribute with its translations.

        Args:
            attribute_id: Owning attribute.
            payload: Per-language display texts.

        Returns:
            AttributeValue: The persisted value.

        Raises:
            AdminNotFoundError: If the attribute does not exist.
        """
        await self._get_attribute(attribute_id)
        value = AttributeValue(attribute_id=attribute_id)
        self.session.add(value)
        await self.session.flush()
        for translation in payload.translations:
            self._add_value_translation(value.id, translation)
        await self.session.commit()
        await self.session.refresh(value)
        return value

    def _add_value_translation(
        self,
        value_id: int,
        payload: AttributeValueTranslationIn,
    ) -> None:
        """Stage an attribute-value translation row for insertion."""
        self.session.add(
            AttributeValueTranslation(
                value_id=value_id,
                lang=payload.lang,
                value=payload.value,
            )
        )

    async def update_attribute_value(
        self,
        value_id: int,
        payload: AttributeValueUpdate,
    ) -> AttributeValue:
        """Replace an attribute value's translations.

        Args:
            value_id: Value to update.
            payload: New translations.

        Returns:
            AttributeValue: The updated value.

        Raises:
            AdminNotFoundError: If the value does not exist.
        """
        value = await self.session.get(AttributeValue, value_id)
        if value is None:
            raise AdminNotFoundError(f"Attribute value {value_id} not found")
        if payload.translations is not None:
            await self.session.execute(
                delete(AttributeValueTranslation).where(
                    AttributeValueTranslation.value_id == value_id
                )
            )
            for translation in payload.translations:
                self._add_value_translation(value_id, translation)
        await self.session.commit()
        await self.session.refresh(value)
        return value

    async def delete_attribute_value(self, value_id: int) -> None:
        """Delete an attribute value, its translations and product links.

        Args:
            value_id: Value to delete.

        Raises:
            AdminNotFoundError: If the value does not exist.
        """
        value = await self.session.get(AttributeValue, value_id)
        if value is None:
            raise AdminNotFoundError(f"Attribute value {value_id} not found")
        await self.session.execute(
            delete(ProductAttribute).where(ProductAttribute.value_id == value_id)
        )
        await self.session.execute(
            delete(AttributeValueTranslation).where(
                AttributeValueTranslation.value_id == value_id
            )
        )
        await self.session.delete(value)
        await self.session.commit()

    async def get_attribute_detail(
        self,
        attribute_id: int,
    ) -> tuple[
        Attribute,
        list[AttributeTranslation],
        list[tuple[AttributeValue, list[AttributeValueTranslation]]],
    ]:
        """Assemble an attribute with its translations and values (for responses).

        Args:
            attribute_id: Attribute to load.

        Returns:
            tuple: ``(attribute, translations, [(value, value_translations)])``.

        Raises:
            AdminNotFoundError: If the attribute does not exist.
        """
        attribute = await self._get_attribute(attribute_id)
        translations = list(
            (
                await self.session.execute(
                    select(AttributeTranslation).where(
                        AttributeTranslation.attribute_id == attribute_id
                    )
                )
            )
            .scalars()
            .all()
        )
        values = list(
            (
                await self.session.execute(
                    select(AttributeValue).where(
                        AttributeValue.attribute_id == attribute_id
                    )
                )
            )
            .scalars()
            .all()
        )
        # Batch all value-translations in one query (was N+1: one per value).
        value_ids = [value.id for value in values]
        grouped: dict[int, list[AttributeValueTranslation]] = {}
        if value_ids:
            vt_rows = (
                (
                    await self.session.execute(
                        select(AttributeValueTranslation).where(
                            AttributeValueTranslation.value_id.in_(value_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            for vt_row in vt_rows:
                grouped.setdefault(vt_row.value_id, []).append(vt_row)
        value_rows: list[tuple[AttributeValue, list[AttributeValueTranslation]]] = [
            (value, grouped.get(value.id, [])) for value in values
        ]
        return attribute, translations, value_rows
