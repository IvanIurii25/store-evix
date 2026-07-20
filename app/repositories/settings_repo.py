"""Data-access layer for :class:`SiteSetting` key/value rows (admin §6.4).

Pure SQL/ORM access — no business rules, no HTTP. Committing is the caller's
(service's) responsibility. The settings service owns which keys exist and how a
typed group (e.g. the SEO block) maps to/from these rows.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import SiteSetting


class SettingsRepository:
    """Async data access for the ``site_setting`` key/value store."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used for all queries.

        Args:
            session: The active async DB session.
        """
        self.session = session

    async def get_map(self, keys: list[str]) -> dict[str, str]:
        """Return the ``{key: value}`` rows present among ``keys``.

        Absent keys are simply missing from the result (the service supplies
        defaults), so a fresh install with no rows returns an empty map.

        Args:
            keys: The setting keys to fetch.

        Returns:
            dict[str, str]: Present ``key -> value`` pairs.
        """
        if not keys:
            return {}
        stmt = select(SiteSetting).where(SiteSetting.key.in_(keys))
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.key: row.value for row in rows}

    async def upsert(self, key: str, value: str) -> None:
        """Insert or update one setting row (create on first write).

        Args:
            key: The setting key.
            value: The new value.
        """
        row = await self.session.get(SiteSetting, key)
        if row is None:
            self.session.add(SiteSetting(key=key, value=value))
        else:
            row.value = value
        await self.session.flush()
