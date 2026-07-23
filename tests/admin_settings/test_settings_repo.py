"""Direct data-access tests for :class:`SettingsRepository` (admin §6.4).

Exercises the key/value store branches without HTTP: empty-keys short circuit,
present-vs-absent rows in ``get_map``, and both ``upsert`` paths (insert a new
row, then update an existing one). Each test is one concept, AAA-structured.

Run with ``EVIX_TEST_DB=evix_test_admin_settings``.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import SiteSetting
from app.repositories.settings_repo import SettingsRepository
from tests.factories import SiteSettingFactory, persist

pytestmark = pytest.mark.asyncio

# A concrete key/value pair reused across the scenarios.
_KEY = "seo.title_ru"
_ABSENT_KEY = "seo.title_ro"
_VALUE = "Магазин evix"
_UPDATED_VALUE = "Обновлённый магазин"


class TestSettingsRepoGetMap:
    """Branches of :meth:`SettingsRepository.get_map`."""

    async def test_get_map_empty_keys_returns_empty_dict(
        self, db_session: AsyncSession
    ) -> None:
        """An empty key list short-circuits to an empty map (no query)."""
        # Arrange: repo bound to the test session.
        repo = SettingsRepository(db_session)

        # Act: ask for nothing.
        result = await repo.get_map([])

        # Assert: empty map, not a DB round-trip.
        assert result == {}, "empty keys must yield an empty map"

    async def test_get_map_returns_only_present_rows(
        self, db_session: AsyncSession
    ) -> None:
        """Present keys are returned; absent keys are simply omitted."""
        # Arrange: one row exists; another requested key does not.
        await persist(db_session, SiteSettingFactory(key=_KEY, value=_VALUE))
        repo = SettingsRepository(db_session)

        # Act: request both the present and the absent key.
        result = await repo.get_map([_KEY, _ABSENT_KEY])

        # Assert: only the present key appears, with its stored value.
        assert result == {_KEY: _VALUE}, "only present rows should be mapped"


class TestSettingsRepoUpsert:
    """Both branches of :meth:`SettingsRepository.upsert` (insert / update)."""

    async def test_upsert_new_key_inserts_row(self, db_session: AsyncSession) -> None:
        """Upserting an absent key creates the row on first write."""
        # Arrange: no row for the key yet.
        repo = SettingsRepository(db_session)

        # Act: first write for the key.
        await repo.upsert(_KEY, _VALUE)

        # Assert: a fresh row now holds the value.
        row = await db_session.get(SiteSetting, _KEY)
        assert row is not None, "upsert must insert a row for a new key"
        assert row.value == _VALUE, "inserted row must carry the given value"

    async def test_upsert_existing_key_updates_value(
        self, db_session: AsyncSession
    ) -> None:
        """Upserting an existing key updates the value in place, not a new row."""
        # Arrange: a row already exists for the key.
        await persist(db_session, SiteSettingFactory(key=_KEY, value=_VALUE))
        repo = SettingsRepository(db_session)

        # Act: upsert the same key with a new value.
        await repo.upsert(_KEY, _UPDATED_VALUE)

        # Assert: the same row now holds the updated value.
        row = await db_session.get(SiteSetting, _KEY)
        assert row is not None, "existing row must remain present"
        assert row.value == _UPDATED_VALUE, "upsert must update the value in place"
