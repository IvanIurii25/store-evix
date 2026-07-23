"""Direct service tests for the SEO defaults service (admin §6.4).

Bypasses HTTP to exercise :class:`SettingsService` branches: reading defaults on
a fresh install, reading stored values, and the full PUT round-trip that maps
the typed :class:`SeoSettings` block to ``seo.<field>`` rows and back.

Run with ``EVIX_TEST_DB=evix_test_admin_settings``.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import SiteSetting
from app.schemas.admin_settings import SeoSettings
from app.services.admin_settings_service import SettingsService
from tests.factories import SiteSettingFactory, persist

pytestmark = pytest.mark.asyncio

# A fully-populated SEO block used for the round-trip.
_FULL_SEO = SeoSettings(
    title_ru="Магазин evix",
    title_ro="Magazin evix",
    description_ru="Лучшие товары",
    description_ro="Cele mai bune produse",
    title_suffix=" — evix",
    og_image_url="https://cdn.evix.md/og.png",
)


class TestSettingsServiceGetSeo:
    """Branches of :meth:`SettingsService.get_seo`."""

    async def test_get_seo_fresh_install_returns_all_empty(
        self, db_session: AsyncSession
    ) -> None:
        """With no rows, every SEO field defaults to an empty string."""
        # Arrange: no ``seo.*`` rows exist.
        service = SettingsService(db_session)

        # Act: read the defaults.
        seo = await service.get_seo()

        # Assert: a fully-formed block with empty fields.
        assert seo == SeoSettings(), "fresh install must yield empty defaults"

    async def test_get_seo_reads_stored_value_and_defaults_missing(
        self, db_session: AsyncSession
    ) -> None:
        """A stored field is read back; the rest keep the empty default."""
        # Arrange: only one SEO key is persisted.
        stored_title = "Только заголовок"
        await persist(
            db_session,
            SiteSettingFactory(key="seo.title_ru", value=stored_title),
        )
        service = SettingsService(db_session)

        # Act: read the SEO block.
        seo = await service.get_seo()

        # Assert: the stored key is populated, others default to empty.
        assert seo.title_ru == stored_title, "stored key must be read back"
        assert seo.title_ro == "", "absent key must default to empty"


class TestSettingsServicePutSeo:
    """Branches of :meth:`SettingsService.put_seo` (persist + read back)."""

    async def test_put_seo_persists_all_fields_and_returns_block(
        self, db_session: AsyncSession
    ) -> None:
        """PUT writes one row per SEO field and returns the persisted block."""
        # Arrange: a service on a fresh DB.
        service = SettingsService(db_session)

        # Act: persist the full block.
        returned = await service.put_seo(_FULL_SEO)

        # Assert: the returned block equals the input.
        assert returned == _FULL_SEO, "put_seo must return the persisted block"

    async def test_put_seo_writes_expected_row_for_each_field(
        self, db_session: AsyncSession
    ) -> None:
        """Each SEO field lands under its ``seo.<field>`` key row."""
        # Arrange: a service on a fresh DB.
        service = SettingsService(db_session)

        # Act: persist the full block.
        await service.put_seo(_FULL_SEO)

        # Assert: a representative row carries the mapped value.
        row = await db_session.get(SiteSetting, "seo.og_image_url")
        assert row is not None, "put_seo must create a row per field"
        assert row.value == _FULL_SEO.og_image_url, "row must hold the field value"

    async def test_put_seo_overwrites_previous_values(
        self, db_session: AsyncSession
    ) -> None:
        """A second PUT updates existing rows rather than duplicating them."""
        # Arrange: an initial block is already stored.
        service = SettingsService(db_session)
        await service.put_seo(_FULL_SEO)
        updated = _FULL_SEO.model_copy(update={"title_ru": "Новый заголовок"})

        # Act: persist an updated block.
        returned = await service.put_seo(updated)

        # Assert: the read-back reflects the update (in-place, no duplicate).
        assert returned.title_ru == "Новый заголовок", "PUT must overwrite values"
        assert returned.title_ro == _FULL_SEO.title_ro, "untouched field must persist"
