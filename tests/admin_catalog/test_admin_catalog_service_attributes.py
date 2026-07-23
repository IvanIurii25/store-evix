"""Service-layer tests for AdminCatalogService attribute operations (B7, §10).

Direct :class:`AdminCatalogService` tests for attribute + value CRUD: not-found
branches, code / translation replacement on update, value create/update/delete
with their cascades, and the batched ``get_attribute_detail`` assembler.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import (
    AttributeValue,
    AttributeValueTranslation,
    ProductAttribute,
)
from app.schemas.admin_catalog import (
    AttributeCreate,
    AttributeTranslationIn,
    AttributeUpdate,
    AttributeValueCreate,
    AttributeValueTranslationIn,
    AttributeValueUpdate,
)
from app.services.admin_catalog_service import (
    AdminCatalogService,
    AdminNotFoundError,
)
from tests.factories import (
    AttributeFactory,
    AttributeTranslationFactory,
    AttributeValueFactory,
    AttributeValueTranslationFactory,
    ProductAttributeFactory,
    create_product,
    persist,
)

pytestmark = pytest.mark.asyncio

# Ids never persisted — used to trigger not-found branches.
MISSING_ATTRIBUTE_ID: int = 444001
MISSING_VALUE_ID: int = 444002


class TestAttributeCrud:
    """Attribute create / update / delete error + success branches."""

    async def test_get_attribute_missing_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service._get_attribute(MISSING_ATTRIBUTE_ID)

    async def test_create_attribute_persists_translations(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)
        payload = AttributeCreate(
            code="material",
            translations=[
                AttributeTranslationIn(lang="ru", name="Материал"),
                AttributeTranslationIn(lang="ro", name="Material"),
            ],
        )

        # Act
        attribute = await service.create_attribute(payload)

        # Assert
        _, translations, _ = await service.get_attribute_detail(attribute.id)
        assert {tr.lang for tr in translations} == {"ru", "ro"}

    async def test_update_attribute_missing_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.update_attribute(
                MISSING_ATTRIBUTE_ID, AttributeUpdate(code="x")
            )

    async def test_update_attribute_code_only(self, db_session: AsyncSession) -> None:
        # Arrange — an attribute whose code we rename (translations untouched).
        attribute = await persist(db_session, AttributeFactory())
        await persist(
            db_session, AttributeTranslationFactory(attribute_id=attribute.id)
        )
        service = AdminCatalogService(db_session)

        # Act — only ``code`` in payload → translations left as-is.
        updated = await service.update_attribute(
            attribute.id, AttributeUpdate(code="renamed")
        )

        # Assert
        assert updated.code == "renamed"
        _, translations, _ = await service.get_attribute_detail(attribute.id)
        assert len(translations) == 1, "translations must be preserved"

    async def test_update_attribute_replaces_translations(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — an attribute with one ru translation.
        attribute = await persist(db_session, AttributeFactory())
        await persist(
            db_session,
            AttributeTranslationFactory(attribute_id=attribute.id, lang="ru"),
        )
        service = AdminCatalogService(db_session)

        # Act — translations provided → old set deleted, new inserted.
        await service.update_attribute(
            attribute.id,
            AttributeUpdate(
                translations=[AttributeTranslationIn(lang="ro", name="Nou")]
            ),
        )

        # Assert — only the new ``ro`` translation remains.
        _, translations, _ = await service.get_attribute_detail(attribute.id)
        assert {tr.lang for tr in translations} == {"ro"}

    async def test_delete_attribute_missing_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.delete_attribute(MISSING_ATTRIBUTE_ID)

    async def test_delete_attribute_cascades_values_and_links(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — attribute with a value, a value-translation, a product link.
        product = await create_product(db_session, is_active=False)
        attribute = await persist(db_session, AttributeFactory())
        value = await persist(
            db_session, AttributeValueFactory(attribute_id=attribute.id)
        )
        await persist(
            db_session,
            AttributeValueTranslationFactory(value_id=value.id),
        )
        await persist(
            db_session,
            ProductAttributeFactory(product_id=product.id, value_id=value.id),
        )
        service = AdminCatalogService(db_session)

        # Act
        await service.delete_attribute(attribute.id)

        # Assert — the value and its product link are gone with the attribute.
        assert await db_session.get(AttributeValue, value.id) is None
        links = (
            (
                await db_session.execute(
                    select(ProductAttribute).where(
                        ProductAttribute.value_id == value.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert links == [], "product links must be purged"

    async def test_delete_attribute_without_values(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — an attribute that has no values (skips the value cascade).
        attribute = await persist(db_session, AttributeFactory())
        await persist(
            db_session, AttributeTranslationFactory(attribute_id=attribute.id)
        )
        service = AdminCatalogService(db_session)

        # Act
        await service.delete_attribute(attribute.id)

        # Assert
        from app.models.catalog import Attribute

        assert await db_session.get(Attribute, attribute.id) is None


class TestAttributeValueCrud:
    """Attribute-value create / update / delete branches."""

    async def test_create_value_missing_attribute_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)
        payload = AttributeValueCreate(
            translations=[AttributeValueTranslationIn(lang="ru", value="Синий")]
        )

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.create_attribute_value(MISSING_ATTRIBUTE_ID, payload)

    async def test_create_value_persists_translations(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        attribute = await persist(db_session, AttributeFactory())
        service = AdminCatalogService(db_session)
        payload = AttributeValueCreate(
            translations=[
                AttributeValueTranslationIn(lang="ru", value="Синий"),
                AttributeValueTranslationIn(lang="ro", value="Albastru"),
            ]
        )

        # Act
        value = await service.create_attribute_value(attribute.id, payload)

        # Assert
        rows = (
            (
                await db_session.execute(
                    select(AttributeValueTranslation).where(
                        AttributeValueTranslation.value_id == value.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {row.lang for row in rows} == {"ru", "ro"}

    async def test_update_value_missing_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.update_attribute_value(
                MISSING_VALUE_ID, AttributeValueUpdate(translations=[])
            )

    async def test_update_value_replaces_translations(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a value with one ru translation.
        attribute = await persist(db_session, AttributeFactory())
        value = await persist(
            db_session, AttributeValueFactory(attribute_id=attribute.id)
        )
        await persist(
            db_session,
            AttributeValueTranslationFactory(value_id=value.id, lang="ru"),
        )
        service = AdminCatalogService(db_session)

        # Act — new translation set replaces the old one.
        await service.update_attribute_value(
            value.id,
            AttributeValueUpdate(
                translations=[AttributeValueTranslationIn(lang="ro", value="Verde")]
            ),
        )

        # Assert
        rows = (
            (
                await db_session.execute(
                    select(AttributeValueTranslation).where(
                        AttributeValueTranslation.value_id == value.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {row.lang for row in rows} == {"ro"}

    async def test_update_value_none_translations_is_noop(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a value with an existing translation; payload omits them.
        attribute = await persist(db_session, AttributeFactory())
        value = await persist(
            db_session, AttributeValueFactory(attribute_id=attribute.id)
        )
        await persist(db_session, AttributeValueTranslationFactory(value_id=value.id))
        service = AdminCatalogService(db_session)

        # Act — translations=None → the existing set is left untouched.
        await service.update_attribute_value(value.id, AttributeValueUpdate())

        # Assert
        rows = (
            (
                await db_session.execute(
                    select(AttributeValueTranslation).where(
                        AttributeValueTranslation.value_id == value.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, "translations must be preserved on a None update"

    async def test_delete_value_missing_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.delete_attribute_value(MISSING_VALUE_ID)

    async def test_delete_value_cascades_translations_and_links(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a value with a translation and a product link.
        product = await create_product(db_session, is_active=False)
        attribute = await persist(db_session, AttributeFactory())
        value = await persist(
            db_session, AttributeValueFactory(attribute_id=attribute.id)
        )
        await persist(db_session, AttributeValueTranslationFactory(value_id=value.id))
        await persist(
            db_session,
            ProductAttributeFactory(product_id=product.id, value_id=value.id),
        )
        service = AdminCatalogService(db_session)

        # Act
        await service.delete_attribute_value(value.id)

        # Assert
        assert await db_session.get(AttributeValue, value.id) is None


class TestAttributeDetail:
    """The batched get_attribute_detail read-model assembler."""

    async def test_detail_missing_attribute_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.get_attribute_detail(MISSING_ATTRIBUTE_ID)

    async def test_detail_groups_value_translations(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — attribute + 2 values, each with a translation.
        attribute = await persist(db_session, AttributeFactory())
        await persist(
            db_session, AttributeTranslationFactory(attribute_id=attribute.id)
        )
        value_a = await persist(
            db_session, AttributeValueFactory(attribute_id=attribute.id)
        )
        value_b = await persist(
            db_session, AttributeValueFactory(attribute_id=attribute.id)
        )
        await persist(db_session, AttributeValueTranslationFactory(value_id=value_a.id))
        await persist(db_session, AttributeValueTranslationFactory(value_id=value_b.id))
        service = AdminCatalogService(db_session)

        # Act
        attr, translations, value_rows = await service.get_attribute_detail(
            attribute.id
        )

        # Assert — each value carries its own translation bucket.
        assert attr.id == attribute.id
        assert len(translations) == 1
        assert len(value_rows) == 2
        assert all(len(vts) == 1 for _, vts in value_rows)

    async def test_detail_attribute_without_values(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — attribute with a translation but no values (skips vt batch).
        attribute = await persist(db_session, AttributeFactory())
        await persist(
            db_session, AttributeTranslationFactory(attribute_id=attribute.id)
        )
        service = AdminCatalogService(db_session)

        # Act
        _, _, value_rows = await service.get_attribute_detail(attribute.id)

        # Assert
        assert value_rows == [], "no values → empty value list"
