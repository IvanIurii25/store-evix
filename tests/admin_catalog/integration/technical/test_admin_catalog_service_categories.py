"""Service-layer tests for AdminCatalogService category operations (B7, §10).

Direct :class:`AdminCatalogService` tests targeting error branches and read
helpers that the happy-path API tests do not exercise: not-found on every
category lookup, delete conflicts / success, translation upsert (insert vs
update), reorder, move-to-cycle rejection, and the translation read helpers.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import CategoryTranslation
from app.schemas.admin_catalog import (
    CategoryCreate,
    CategoryReorderItem,
    CategoryTranslationIn,
    CategoryUpdate,
)
from app.services.admin_catalog_service import (
    AdminCatalogService,
    AdminConflictError,
    AdminNotFoundError,
    AdminValidationError,
)
from tests.factories import (
    CategoryFactory,
    create_category,
    create_product,
    persist,
)

pytestmark = pytest.mark.asyncio

# A category id that is never persisted — used to trigger not-found branches.
MISSING_CATEGORY_ID: int = 987654


def _tr(lang: str, name: str, slug: str) -> CategoryTranslationIn:
    """Build a category translation payload."""
    return CategoryTranslationIn(lang=lang, name=name, slug=slug)


class TestCategoryCreate:
    """create_category success path (path/depth derivation + translations)."""

    async def test_create_root_derives_path_and_translations(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a root category with both translations.
        service = AdminCatalogService(db_session)
        payload = CategoryCreate(
            translations=[
                _tr("ru", "Root RU", "root-ru"),
                _tr("ro", "Root RO", "root-ro"),
            ]
        )

        # Act
        category = await service.create_category(payload)

        # Assert — path becomes [own id], depth 0, both translations attached.
        assert category.path == [category.id]
        assert category.depth == 0
        rows = await service.get_category_translations(category.id)
        assert {row.lang for row in rows} == {"ru", "ro"}

    async def test_create_child_prefixes_parent_path(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a persisted root to parent the new node under.
        parent = await create_category(db_session)
        service = AdminCatalogService(db_session)
        payload = CategoryCreate(
            parent_id=parent.id, translations=[_tr("ru", "Child", "child-ru")]
        )

        # Act
        child = await service.create_category(payload)

        # Assert — the child's path is parent.path + [child id], depth 1.
        assert child.path == [parent.id, child.id]
        assert child.depth == 1


class TestCategoryLookup:
    """Not-found branches on category lookup / parent resolution."""

    async def test_get_category_missing_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert — the private loader raises the mapped domain error.
        with pytest.raises(AdminNotFoundError):
            await service._get_category(MISSING_CATEGORY_ID)

    async def test_create_category_missing_parent_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a create payload pointing at a non-existent parent.
        service = AdminCatalogService(db_session)
        payload = CategoryCreate(
            parent_id=MISSING_CATEGORY_ID,
            translations=[_tr("ru", "n", "slug-ru")],
        )

        # Act / Assert — _resolve_path surfaces the missing parent.
        with pytest.raises(AdminNotFoundError):
            await service.create_category(payload)

    async def test_update_category_missing_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.update_category(
                MISSING_CATEGORY_ID, CategoryUpdate(position=3)
            )


class TestCategoryUpdate:
    """Structural update paths including the parent-change delegation."""

    async def test_update_position_only_persists_field(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        category = await create_category(db_session)
        service = AdminCatalogService(db_session)
        new_position = 7

        # Act — no parent_id in the payload → straight field set, no move.
        updated = await service.update_category(
            category.id, CategoryUpdate(position=new_position)
        )

        # Assert
        assert updated.position == new_position, "position must be applied"

    async def test_update_with_parent_change_delegates_to_move(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a root and a separate node to re-parent under it.
        parent = await create_category(db_session)
        node = await create_category(db_session)
        service = AdminCatalogService(db_session)

        # Act — parent_id present in payload → delegates to move_category.
        updated = await service.update_category(
            node.id, CategoryUpdate(parent_id=parent.id, position=2)
        )

        # Assert — path recomputed by move; the trailing field applied too.
        assert updated.path == [parent.id, node.id], "move must recompute path"
        assert updated.position == 2, "residual field must still apply"


class TestCategoryDelete:
    """Delete guards (children / products) and the success path."""

    async def test_delete_missing_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.delete_category(MISSING_CATEGORY_ID)

    async def test_delete_with_child_categories_conflict(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a parent with one child category.
        parent = await create_category(db_session)
        await create_category(db_session, parent_id=parent.id)
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminConflictError):
            await service.delete_category(parent.id)

    async def test_delete_with_products_conflict(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a category that still owns a product.
        category = await create_category(db_session)
        await create_product(db_session, category=category)
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminConflictError):
            await service.delete_category(category.id)

    async def test_delete_empty_category_removes_it_and_translations(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a leaf category with two translations, no children/products.
        category = await create_category(db_session)
        service = AdminCatalogService(db_session)

        # Act
        await service.delete_category(category.id)

        # Assert — the row is gone and its translations were purged.
        assert await db_session.get(type(category), category.id) is None
        remaining = (
            (
                await db_session.execute(
                    select(CategoryTranslation).where(
                        CategoryTranslation.category_id == category.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert remaining == [], "translations must be deleted with the category"


class TestCategoryTranslation:
    """Translation upsert (insert + update) and read helpers."""

    async def test_set_translation_missing_category_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.set_category_translation(
                MISSING_CATEGORY_ID, _tr("ru", "n", "slug-ru")
            )

    async def test_set_translation_inserts_when_absent(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a category with no translation in the target language.
        category = await create_category(db_session, langs=())
        service = AdminCatalogService(db_session)

        # Act
        result = await service.set_category_translation(
            category.id, _tr("ru", "New RU", "new-ru")
        )

        # Assert
        assert result.name == "New RU", "a fresh translation row is inserted"

    async def test_set_translation_updates_when_present(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a category that already has a ``ru`` translation.
        category = await create_category(db_session, langs=("ru",))
        service = AdminCatalogService(db_session)

        # Act — same lang → the existing row is updated, not duplicated.
        result = await service.set_category_translation(
            category.id, _tr("ru", "Updated", "updated-ru")
        )

        # Assert
        assert result.slug == "updated-ru", "existing row must be updated"
        rows = await service.get_category_translations(category.id)
        assert len(rows) == 1, "no duplicate translation row is created"

    async def test_get_category_translations_returns_all(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a category with both languages.
        category = await create_category(db_session, langs=("ru", "ro"))
        service = AdminCatalogService(db_session)

        # Act
        rows = await service.get_category_translations(category.id)

        # Assert
        assert {row.lang for row in rows} == {"ru", "ro"}

    async def test_get_category_out_data_empty_returns_empty_dict(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act — the empty-input short-circuit.
        result = await service.get_category_out_data([])

        # Assert
        assert result == {}, "no categories → empty grouping"

    async def test_get_category_out_data_groups_by_category(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — two categories, each with translations.
        first = await create_category(db_session, langs=("ru", "ro"))
        second = await create_category(db_session, langs=("ru",))
        service = AdminCatalogService(db_session)

        # Act
        grouped = await service.get_category_out_data([first, second])

        # Assert — one bucket per id, translations attached to the right owner.
        assert len(grouped[first.id]) == 2
        assert len(grouped[second.id]) == 1


class TestCategoryReorderMove:
    """Reorder and move-cycle error branches."""

    async def test_reorder_assigns_positions(self, db_session: AsyncSession) -> None:
        # Arrange — two categories to renumber.
        first = await create_category(db_session)
        second = await create_category(db_session)
        service = AdminCatalogService(db_session)

        # Act
        await service.reorder_categories(
            [
                CategoryReorderItem(category_id=first.id, position=5),
                CategoryReorderItem(category_id=second.id, position=9),
            ]
        )

        # Assert
        await db_session.refresh(first)
        await db_session.refresh(second)
        assert (first.position, second.position) == (5, 9)

    async def test_reorder_missing_category_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.reorder_categories(
                [CategoryReorderItem(category_id=MISSING_CATEGORY_ID, position=1)]
            )

    async def test_move_under_own_subtree_raises_validation(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — root -> child; try to move root under its own child.
        root = await create_category(db_session)
        child = await persist(
            db_session,
            CategoryFactory(parent_id=root.id, path=[root.id], depth=1),
        )
        child.path = [root.id, child.id]
        await db_session.flush()
        service = AdminCatalogService(db_session)

        # Act / Assert — the new parent is a descendant → cycle rejected.
        with pytest.raises(AdminValidationError):
            await service.move_category(root.id, child.id)

    async def test_move_missing_node_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.move_category(MISSING_CATEGORY_ID, None)

    async def test_move_rewrites_descendant_subtree(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — root -> mid -> leaf, plus a separate target root.
        root = await create_category(db_session)
        mid = await persist(
            db_session,
            CategoryFactory(parent_id=root.id, path=[root.id], depth=1),
        )
        mid.path = [root.id, mid.id]
        leaf = await persist(
            db_session,
            CategoryFactory(parent_id=mid.id, path=[root.id, mid.id], depth=2),
        )
        await db_session.flush()
        leaf.path = [root.id, mid.id, leaf.id]
        await db_session.flush()
        target = await create_category(db_session)
        service = AdminCatalogService(db_session)

        # Act — move ``mid`` (with its ``leaf`` descendant) under ``target``.
        await service.move_category(mid.id, target.id)

        # Assert — the descendant leaf's path/depth are rewritten too.
        await db_session.refresh(leaf)
        assert leaf.path == [target.id, mid.id, leaf.id]
        assert leaf.depth == 2
