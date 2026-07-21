"""Back-office write logic for the catalog (stage B7).

Thin, explicit admin operations over the catalog: category CRUD (+ translations,
reorder, move-with-subtree-path-recompute), product CRUD (+ translations, media
uploads, attribute links, activation) and attribute CRUD (+ values). It reuses
:class:`~app.services.catalog_service.CatalogService` for the publication rule
and the ``product_card`` read-model rebuild — those invariants live in exactly
one place (§10). No HTTP knowledge: the router maps the raised exceptions.
"""

from fastapi import UploadFile
from sqlalchemy import BigInteger, cast, delete, func, or_, select
from sqlalchemy.dialects.postgresql import ARRAY, array
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.storage import get_storage
from app.models.catalog import (
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
)
from app.services.catalog_service import CatalogService

# Default media kind for v1 uploads (only images are allowed — §2.1).
DEFAULT_MEDIA_KIND: str = "image"

# Stock at or below this is surfaced as "low stock" in the back-office (§4.2).
LOW_STOCK_THRESHOLD: int = 5


class AdminCatalogError(Exception):
    """Base class for admin-catalog domain errors (mapped to HTTP by the router)."""

    code: str = "admin_catalog_error"


class AdminNotFoundError(AdminCatalogError):
    """A referenced admin resource does not exist."""

    code = "not_found"


class AdminConflictError(AdminCatalogError):
    """A write would violate a uniqueness / relational invariant."""

    code = "conflict"


class AdminValidationError(AdminCatalogError):
    """A payload is structurally valid but semantically rejected."""

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
            is_active=payload.is_active,
            is_featured=payload.is_featured,
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
        return product

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
        # Drop the read-model rows (product may have been active).
        await self.catalog.repo.delete_product_cards(product_id)
        await self.session.delete(product)
        await self.session.commit()

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
        return unique_ids

    async def add_product_media(
        self,
        product_id: int,
        upload: UploadFile,
    ) -> Media:
        """Store an uploaded image and record a :class:`Media` row.

        The bytes are read from the upload and handed to the configured storage
        backend (:func:`~app.core.storage.get_storage` — local dir or S3/MinIO);
        the returned public URL is stored as the DB ``url`` (v1: one url, no
        derivatives).

        Args:
            product_id: Product the image belongs to.
            upload: The uploaded file.

        Returns:
            Media: The persisted media row.

        Raises:
            AdminNotFoundError: If the product does not exist.
            AdminValidationError: If the file is not an allowed image kind.
        """
        await self._get_product(product_id)
        if not self._is_allowed_image(upload):
            raise AdminValidationError("Only image uploads are allowed")

        await upload.seek(0)
        data = await upload.read()
        storage = get_storage()
        url = await storage.save(
            data,
            filename=upload.filename or "",
            content_type=upload.content_type or "application/octet-stream",
        )
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

        Returns:
            list[tuple[Product, str | None]]: ``(product, matched_name)`` rows.
        """
        stmt = (
            select(Product, ProductTranslation.name)
            .outerjoin(
                ProductTranslation,
                ProductTranslation.product_id == Product.id,
            )
            .order_by(Product.id.desc())
            .limit(limit)
        )
        if search:
            term = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(
                    Product.code.ilike(term),
                    ProductTranslation.name.ilike(term),
                )
            )
        if is_active is not None:
            stmt = stmt.where(Product.is_active.is_(is_active))
        if low_stock:
            stmt = stmt.where(Product.qty <= LOW_STOCK_THRESHOLD)
        if on_sale:
            stmt = stmt.where(
                Product.old_price.is_not(None),
                Product.old_price > Product.price,
            )
        rows = (await self.session.execute(stmt)).all()
        # Deduplicate on product id (outer join may yield two translation rows).
        seen: dict[int, tuple[Product, str | None]] = {}
        for product, name in rows:
            if product.id not in seen:
                seen[product.id] = (product, name)
        return list(seen.values())

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
