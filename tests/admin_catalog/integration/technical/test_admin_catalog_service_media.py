"""Service-layer tests for AdminCatalogService media operations (B7, §10).

Direct :class:`AdminCatalogService` tests for image add / delete / reorder. The
storage backend and the CPU-bound image validator are the *only* things mocked
(no MinIO, no real Pillow work) — the service, DB and card rebuild are real.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.images import ImageValidationError
from app.models.catalog import Media
from app.services.admin_catalog_service import (
    AdminCatalogService,
    AdminNotFoundError,
    AdminValidationError,
)
from tests.factories import MediaFactory, create_product, persist

pytestmark = pytest.mark.asyncio

MISSING_PRODUCT_ID: int = 333001
MISSING_MEDIA_ID: int = 333002
STORAGE_URL: str = "/media/fake.png"


class _FakeUpload:
    """Minimal :class:`UploadFile` stand-in with a declared content type."""

    def __init__(self, content_type: str, data: bytes = b"bytes") -> None:
        self.content_type = content_type
        self.filename = "photo.png"
        self._data = data

    async def seek(self, offset: int) -> None:
        return None

    async def read(self) -> bytes:
        return self._data


def _fake_storage() -> AsyncMock:
    """Return an AsyncMock storage that records save / save_variants / delete."""
    storage = AsyncMock()
    storage.save.return_value = STORAGE_URL
    storage.save_variants.return_value = None
    storage.delete.return_value = None
    return storage


def _patch_media_deps(storage: AsyncMock):
    """Patch the module-level storage + image validator used by add_product_media."""
    return (
        patch(
            "app.services.admin_catalog_service.get_storage",
            return_value=storage,
        ),
        patch(
            "app.services.admin_catalog_service.validate_and_build_variants",
            return_value={200: b"webp"},
        ),
    )


class TestAddProductMedia:
    """add_product_media: guards + happy path with mocked storage/validator."""

    async def test_missing_product_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)
        upload = _FakeUpload("image/png")

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.add_product_media(MISSING_PRODUCT_ID, upload)  # type: ignore[arg-type]

    async def test_non_image_content_type_raises_validation(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a real product, but the upload is not an image.
        product = await create_product(db_session, is_active=False)
        service = AdminCatalogService(db_session)
        upload = _FakeUpload("text/plain")

        # Act / Assert — the prefix check rejects before any storage call.
        with pytest.raises(AdminValidationError):
            await service.add_product_media(product.id, upload)  # type: ignore[arg-type]

    async def test_undecodable_image_raises_validation(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — image content type, but the validator rejects the bytes.
        product = await create_product(db_session, is_active=False)
        service = AdminCatalogService(db_session)
        upload = _FakeUpload("image/png")
        storage = _fake_storage()

        # Act / Assert — ImageValidationError maps to the domain validation error.
        with (
            patch(
                "app.services.admin_catalog_service.get_storage",
                return_value=storage,
            ),
            patch(
                "app.services.admin_catalog_service.validate_and_build_variants",
                side_effect=ImageValidationError("bad"),
            ),
            pytest.raises(AdminValidationError),
        ):
            await service.add_product_media(product.id, upload)  # type: ignore[arg-type]
        storage.save.assert_not_called()

    async def test_add_media_on_inactive_product_stores_row(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — inactive product: stores the row, skips the card rebuild.
        product = await create_product(db_session, is_active=False)
        service = AdminCatalogService(db_session)
        upload = _FakeUpload("image/png")
        storage = _fake_storage()
        storage_patch, validate_patch = _patch_media_deps(storage)

        # Act
        with storage_patch, validate_patch:
            media = await service.add_product_media(product.id, upload)  # type: ignore[arg-type]

        # Assert — the row was persisted at position 0 with the stored URL.
        assert media.url == STORAGE_URL
        assert media.position == 0
        storage.save.assert_awaited_once()
        storage.save_variants.assert_awaited_once()

    async def test_add_media_on_active_product_appends_position(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — active product that already has one media row (position 0).
        product = await create_product(db_session, is_active=True)
        await persist(db_session, MediaFactory(product_id=product.id, position=0))
        service = AdminCatalogService(db_session)
        upload = _FakeUpload("image/png")
        storage = _fake_storage()
        storage_patch, validate_patch = _patch_media_deps(storage)

        # Act — active product triggers the card rebuild branch too.
        with storage_patch, validate_patch:
            media = await service.add_product_media(product.id, upload)  # type: ignore[arg-type]

        # Assert — the new image is appended after the existing one.
        assert media.position == 1, "next position appends after the max"


class TestDeleteProductMedia:
    """delete_product_media: guards, best-effort storage cleanup, card rebuild."""

    async def test_missing_product_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.delete_product_media(MISSING_PRODUCT_ID, MISSING_MEDIA_ID)

    async def test_missing_media_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — a real product, but no such media row.
        product = await create_product(db_session, is_active=False)
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.delete_product_media(product.id, MISSING_MEDIA_ID)

    async def test_media_of_other_product_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — media that belongs to a different product.
        owner = await create_product(db_session, is_active=False)
        other = await create_product(db_session, is_active=False)
        media = await persist(db_session, MediaFactory(product_id=owner.id))
        service = AdminCatalogService(db_session)

        # Act / Assert — the ownership check rejects the cross-product delete.
        with pytest.raises(AdminNotFoundError):
            await service.delete_product_media(other.id, media.id)

    async def test_delete_media_active_product_rebuilds_and_cleans_storage(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — active product with one media row; storage delete is mocked.
        product = await create_product(db_session, is_active=True)
        media = await persist(db_session, MediaFactory(product_id=product.id))
        service = AdminCatalogService(db_session)
        storage = _fake_storage()

        # Act
        with patch(
            "app.services.admin_catalog_service.get_storage",
            return_value=storage,
        ):
            await service.delete_product_media(product.id, media.id)

        # Assert — row deleted, best-effort storage removal invoked.
        assert await db_session.get(Media, media.id) is None
        storage.delete.assert_awaited_once_with(media.url)

    async def test_delete_media_swallows_storage_error(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — inactive product; the storage delete will raise.
        product = await create_product(db_session, is_active=False)
        media = await persist(db_session, MediaFactory(product_id=product.id))
        service = AdminCatalogService(db_session)
        storage = _fake_storage()
        storage.delete.side_effect = RuntimeError("storage down")

        # Act — the committed row deletion must survive the storage failure.
        with patch(
            "app.services.admin_catalog_service.get_storage",
            return_value=storage,
        ):
            await service.delete_product_media(product.id, media.id)

        # Assert
        assert await db_session.get(Media, media.id) is None


class TestReorderProductMedia:
    """reorder_product_media: permutation guard + reorder / rebuild."""

    async def test_missing_product_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminNotFoundError):
            await service.reorder_product_media(MISSING_PRODUCT_ID, [1])

    async def test_non_permutation_raises_validation(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — product with two media; pass only one id (not a permutation).
        product = await create_product(db_session, is_active=False)
        first = await persist(db_session, MediaFactory(product_id=product.id))
        await persist(db_session, MediaFactory(product_id=product.id))
        service = AdminCatalogService(db_session)

        # Act / Assert
        with pytest.raises(AdminValidationError):
            await service.reorder_product_media(product.id, [first.id])

    async def test_reorder_inactive_product_sets_positions(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — inactive product with two media rows.
        product = await create_product(db_session, is_active=False)
        first = await persist(
            db_session, MediaFactory(product_id=product.id, position=0)
        )
        second = await persist(
            db_session, MediaFactory(product_id=product.id, position=1)
        )
        service = AdminCatalogService(db_session)

        # Act — reverse the order; positions are re-assigned 0..n.
        result = await service.reorder_product_media(product.id, [second.id, first.id])

        # Assert
        assert [media.id for media in result] == [second.id, first.id]
        assert [media.position for media in result] == [0, 1]

    async def test_reorder_active_product_rebuilds_card(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — active product with two media; reorder triggers the rebuild.
        product = await create_product(db_session, is_active=True)
        first = await persist(
            db_session, MediaFactory(product_id=product.id, position=0)
        )
        second = await persist(
            db_session, MediaFactory(product_id=product.id, position=1)
        )
        service = AdminCatalogService(db_session)

        # Act
        result = await service.reorder_product_media(product.id, [second.id, first.id])

        # Assert — the new main image is first.
        assert result[0].id == second.id


class TestMediaReadHelpers:
    """The plain media read helpers used by responses."""

    async def test_get_product_media_orders_by_position(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange — two out-of-order media rows.
        product = await create_product(db_session, is_active=False)
        await persist(db_session, MediaFactory(product_id=product.id, position=5))
        await persist(db_session, MediaFactory(product_id=product.id, position=1))
        service = AdminCatalogService(db_session)

        # Act
        media = await service.get_product_media(product.id)

        # Assert
        assert [m.position for m in media] == [1, 5]

    async def test_is_allowed_image_accepts_image_prefix(self) -> None:
        # Arrange
        upload = SimpleNamespace(content_type="image/jpeg")

        # Act / Assert — a genuine image content type passes the static check.
        assert AdminCatalogService._is_allowed_image(upload) is True  # type: ignore[arg-type]

    async def test_is_allowed_image_rejects_missing_content_type(self) -> None:
        # Arrange — no content type declared.
        upload = SimpleNamespace(content_type=None)

        # Act / Assert
        assert AdminCatalogService._is_allowed_image(upload) is False  # type: ignore[arg-type]


# Keep the imported ``UploadFile`` symbol referenced for type clarity in helpers.
_ = UploadFile
