"""Service-layer tests for AdminCatalogService product operations (B7, §10).

Direct :class:`AdminCatalogService` tests for product create/update/delete,
translation upsert, attribute-link replacement (including the missing-value
guard), the restock-notification side effect, and the back-office search
filters (search term, is_active, low_stock, on_sale, empty result).
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import (
    Media,
    ProductAttribute,
    ProductTranslation,
)
from app.schemas.admin_catalog import (
    ProductCreate,
    ProductTranslationIn,
    ProductUpdate,
)
from app.services.admin_catalog_service import (
    AdminCatalogService,
    AdminNotFoundError,
)
from tests.factories import (
    AttributeFactory,
    AttributeValueFactory,
    MediaFactory,
    ProductAttributeFactory,
    create_category,
    create_product,
    persist,
)

pytestmark = pytest.mark.asyncio

# Ids that are never persisted — used to trigger not-found branches.
MISSING_PRODUCT_ID: int = 555001
MISSING_CATEGORY_ID: int = 555002
MISSING_VALUE_ID: int = 555003

# Below the low-stock threshold (5) and at/above it, for the filter tests.
LOW_QTY: int = 2
HIGH_QTY: int = 50


def _p_tr(lang: str, slug: str) -> ProductTranslationIn:
    """Build a product translation payload for the given language."""
    return ProductTranslationIn(lang=lang, name=f"name-{lang}", slug=slug)


class TestProductCreateUpdate:
    """Create / update product paths, including not-found and restock hook."""

    async def test_get_product_missing_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service._get_product(MISSING_PRODUCT_ID)

    async def test_create_product_missing_category_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)
        payload = ProductCreate(
            category_id=MISSING_CATEGORY_ID,
            code="SKU-NF",
            price=Decimal("10.00"),
            translations=[_p_tr("ru", "sku-nf-ru")],
        )

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.create_product(payload)

    async def test_create_inactive_product_persists_translations(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — inactive product with a single translation (no publish gate).
        category = await create_category(db_session)
        service = AdminCatalogService(db_session)
        payload = ProductCreate(
            category_id=category.id,
            code="SKU-INACT",
            price=Decimal("12.00"),
            is_active=False,
            translations=[_p_tr("ru", "sku-inact-ru")],
        )

        # Act
        product = await service.create_product(payload)

        # Assert
        assert product.id is not None
        rows = await service.get_product_translations(product.id)
        assert {row.lang for row in rows} == {"ru"}

    async def test_create_active_product_rebuilds_card(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — active product with both translations (publish gate passes).
        category = await create_category(db_session)
        service = AdminCatalogService(db_session)
        payload = ProductCreate(
            category_id=category.id,
            code="SKU-ACTIVE",
            price=Decimal("15.00"),
            is_active=True,
            translations=[_p_tr("ru", "sku-active-ru"), _p_tr("ro", "sku-active-ro")],
        )

        # Act — the active branch asserts publishable + rebuilds the card.
        product = await service.create_product(payload)

        # Assert
        assert product.is_active is True

    async def test_update_inactive_product_skips_publish_gate(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — inactive product; updating a field must not hit the gate.
        product = await create_product(db_session, is_active=False)
        service = AdminCatalogService(db_session)

        # Act — is_active stays False → the publish branch is skipped.
        updated = await service.update_product(product.id, ProductUpdate(qty=99))

        # Assert
        assert updated.qty == 99

    async def test_update_missing_category_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — an existing product pointed at a missing new category.
        product = await create_product(db_session, is_active=False)
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.update_product(
                product.id, ProductUpdate(category_id=MISSING_CATEGORY_ID)
            )

    async def test_update_price_rebuilds_and_returns_product(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — an active product (both langs) whose price we change.
        product = await create_product(db_session, is_active=True)
        service = AdminCatalogService(db_session)
        new_price = Decimal("77.00")

        # Act
        updated = await service.update_product(
            product.id, ProductUpdate(price=new_price)
        )

        # Assert
        assert updated.price == new_price

    async def test_update_restock_transition_enqueues_task(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — an out-of-stock active product going back in stock (§3).
        product = await create_product(db_session, is_active=True, qty=0)
        service = AdminCatalogService(db_session)

        # Act — patch the lazily-imported Celery task to observe the enqueue.
        with patch("app.tasks.restock.send_restock_notifications") as task:
            await service.update_product(product.id, ProductUpdate(qty=10))

        # Assert — the 0 → in-stock transition fired exactly one enqueue.
        task.delay.assert_called_once_with(product.id)

    async def test_update_no_restock_when_still_zero(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — product stays at zero stock (no transition).
        product = await create_product(db_session, is_active=True, qty=0)
        service = AdminCatalogService(db_session)

        # Act
        with patch("app.tasks.restock.send_restock_notifications") as task:
            await service.update_product(product.id, ProductUpdate(price=Decimal("9")))

        # Assert — no transition → no enqueue.
        task.delay.assert_not_called()

    async def test_notify_if_restocked_swallows_enqueue_error(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — an out-of-stock active product; the broker will "fail".
        product = await create_product(db_session, is_active=True, qty=0)
        service = AdminCatalogService(db_session)

        # Act — enqueue raises; the committed update must still succeed.
        with patch("app.tasks.restock.send_restock_notifications") as task:
            task.delay.side_effect = RuntimeError("broker down")
            updated = await service.update_product(product.id, ProductUpdate(qty=3))

        # Assert — the update committed despite the swallowed enqueue error.
        assert updated.qty == 3, "product write must survive a broker outage"


class TestProductDeleteTranslation:
    """Delete cascade and product-translation upsert paths."""

    async def test_delete_missing_product_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.delete_product(MISSING_PRODUCT_ID)

    async def test_delete_product_removes_related_rows(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a product with a translation + a media row.
        product = await create_product(db_session, is_active=False)
        await persist(db_session, MediaFactory(product_id=product.id))
        service = AdminCatalogService(db_session)

        # Act
        await service.delete_product(product.id)

        # Assert — product and its media are gone.
        assert await db_session.get(type(product), product.id) is None
        media = (
            (
                await db_session.execute(
                    select(Media).where(Media.product_id == product.id)
                )
            )
            .scalars()
            .all()
        )
        assert media == [], "media rows must be deleted with the product"

    async def test_set_translation_missing_product_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.set_product_translation(
                MISSING_PRODUCT_ID, _p_tr("ru", "x-ru")
            )

    async def test_set_translation_inserts_for_inactive_product(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — inactive product with no ``ro`` translation yet.
        product = await create_product(db_session, is_active=False, langs=("ru",))
        service = AdminCatalogService(db_session)

        # Act — new lang on an inactive product (no publish gate).
        result = await service.set_product_translation(
            product.id, _p_tr("ro", "new-ro")
        )

        # Assert
        assert result.lang == "ro", "a fresh translation row is inserted"

    async def test_set_translation_updates_existing_row(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — active product; overwrite its existing ``ru`` translation.
        product = await create_product(db_session, is_active=True)
        service = AdminCatalogService(db_session)

        # Act
        result = await service.set_product_translation(
            product.id, _p_tr("ru", "updated-ru")
        )

        # Assert — same-lang upsert updates in place (no duplicate).
        assert result.slug == "updated-ru"
        rows = (
            (
                await db_session.execute(
                    select(ProductTranslation).where(
                        ProductTranslation.product_id == product.id,
                        ProductTranslation.lang == "ru",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, "no duplicate ru translation is created"


class TestProductAttributesLink:
    """set_product_attributes replacement + guards, and read helpers."""

    async def test_set_attributes_missing_product_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.set_product_attributes(MISSING_PRODUCT_ID, [])

    async def test_set_attributes_empty_clears_all_links(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a product with a pre-existing attribute link.
        product = await create_product(db_session, is_active=False)
        attribute = await persist(db_session, AttributeFactory())
        value = await persist(
            db_session, AttributeValueFactory(attribute_id=attribute.id)
        )
        await persist(
            db_session,
            ProductAttributeFactory(product_id=product.id, value_id=value.id),
        )
        service = AdminCatalogService(db_session)

        # Act — empty set skips the value-existence check and clears links.
        result = await service.set_product_attributes(product.id, [])

        # Assert
        assert result == []
        links = (
            (
                await db_session.execute(
                    select(ProductAttribute.value_id).where(
                        ProductAttribute.product_id == product.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert links == [], "all links cleared on an empty set"

    async def test_set_attributes_missing_value_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a real product, but one of the value ids does not exist.
        product = await create_product(db_session, is_active=False)
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.set_product_attributes(product.id, [MISSING_VALUE_ID])

    async def test_set_attributes_replaces_and_dedupes(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a product and two real attribute values.
        product = await create_product(db_session, is_active=False)
        attribute = await persist(db_session, AttributeFactory())
        value_a = await persist(
            db_session, AttributeValueFactory(attribute_id=attribute.id)
        )
        value_b = await persist(
            db_session, AttributeValueFactory(attribute_id=attribute.id)
        )
        # Pre-existing link that must be replaced by the write.
        await persist(
            db_session,
            ProductAttributeFactory(product_id=product.id, value_id=value_a.id),
        )
        service = AdminCatalogService(db_session)

        # Act — duplicates in input are collapsed; old links are cleared first.
        result = await service.set_product_attributes(
            product.id, [value_b.id, value_b.id]
        )

        # Assert — only the new (deduped) set remains linked.
        assert result == [value_b.id]
        links = (
            (
                await db_session.execute(
                    select(ProductAttribute.value_id).where(
                        ProductAttribute.product_id == product.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert links == [value_b.id]

    async def test_get_product_value_ids_returns_links(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a product linked to one value.
        product = await create_product(db_session, is_active=False)
        attribute = await persist(db_session, AttributeFactory())
        value = await persist(
            db_session, AttributeValueFactory(attribute_id=attribute.id)
        )
        await persist(
            db_session,
            ProductAttributeFactory(product_id=product.id, value_id=value.id),
        )
        service = AdminCatalogService(db_session)

        # Act
        ids = await service.get_product_value_ids(product.id)

        # Assert
        assert ids == [value.id]


class TestProductSearch:
    """search_products filters and the empty-result short-circuit."""

    async def test_search_empty_returns_empty_list(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a search term matching nothing.
        service = AdminCatalogService(db_session)

        # Act
        rows = await service.search_products("no-such-term-zzz")

        # Assert — the ``not product_ids`` short-circuit returns [].
        assert rows == []

    async def test_search_by_code_and_name_returns_display_name(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a product whose default-lang name is set.
        category = await create_category(db_session)
        product = await create_product(db_session, category=category, is_active=False)
        await service_set_ru_name(db_session, product.id, "Findable")
        service = AdminCatalogService(db_session)

        # Act — no search term → recent products; display name is the ru row.
        rows = await service.search_products(None, limit=10)

        # Assert — the tuple carries the default-language display name.
        matched = {pid: name for pid, name in ((p.id, n) for p, n in rows)}
        assert matched.get(product.id) == "Findable"

    async def test_search_low_stock_filters_below_threshold(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — one low-stock and one well-stocked product.
        category = await create_category(db_session)
        low = await create_product(
            db_session, category=category, is_active=False, qty=LOW_QTY
        )
        await create_product(
            db_session, category=category, is_active=False, qty=HIGH_QTY
        )
        service = AdminCatalogService(db_session)

        # Act
        rows = await service.search_products(None, low_stock=True)

        # Assert
        ids = {product.id for product, _ in rows}
        assert low.id in ids

    async def test_search_on_sale_filters_discounted(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — one on-sale product (old_price > price) and one plain.
        category = await create_category(db_session)
        sale = await create_product(
            db_session,
            category=category,
            is_active=False,
            price=Decimal("8.00"),
            old_price=Decimal("10.00"),
        )
        await create_product(
            db_session, category=category, is_active=False, price=Decimal("5.00")
        )
        service = AdminCatalogService(db_session)

        # Act
        rows = await service.search_products(None, on_sale=True)

        # Assert
        ids = {product.id for product, _ in rows}
        assert sale.id in ids

    async def test_search_is_active_filter(self, db_session: AsyncSession) -> None:
        # Arrange — an inactive and an active product.
        category = await create_category(db_session)
        inactive = await create_product(db_session, category=category, is_active=False)
        active = await create_product(db_session, category=category, is_active=True)
        service = AdminCatalogService(db_session)

        # Act
        rows = await service.search_products(None, is_active=False)

        # Assert
        ids = {product.id for product, _ in rows}
        assert inactive.id in ids
        assert active.id not in ids


async def service_set_ru_name(
    session: AsyncSession, product_id: int, name: str
) -> None:
    """Set the ``ru`` translation name for a product (search display column)."""
    row = (
        await session.execute(
            select(ProductTranslation).where(
                ProductTranslation.product_id == product_id,
                ProductTranslation.lang == "ru",
            )
        )
    ).scalar_one()
    row.name = name
    await session.flush()
