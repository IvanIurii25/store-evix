"""Async data-access layer for homepage banners (P0).

Query construction only — the display-window rule and CRUD invariants live in
:class:`~app.services.banner_service.BannerService`.
"""

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.banner import Banner, BannerTranslation


class BannerRepository:
    """Read/write queries for banners, bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used by every query method.

        Args:
            session: Active async session (request- or test-scoped).
        """
        self.session = session

    # ------------------------------------------------------------------ #
    # Public reads
    # ------------------------------------------------------------------ #
    async def list_live(
        self,
        lang: str,
        now: datetime,
    ) -> list[tuple[Banner, BannerTranslation]]:
        """Return banners a visitor should see right now, in display order.

        A banner qualifies when it is active, ``now`` falls inside its window
        (an open end counts as open-ended) and it has a creative for ``lang``.

        Args:
            lang: Requested language code.
            now: Current time, passed in so the rule is testable.

        Returns:
            list[tuple[Banner, BannerTranslation]]: Ordered ``(banner, creative)``.
        """
        stmt = (
            select(Banner, BannerTranslation)
            .join(BannerTranslation, BannerTranslation.banner_id == Banner.id)
            .where(
                Banner.is_active.is_(True),
                BannerTranslation.lang == lang,
                (Banner.starts_at.is_(None)) | (Banner.starts_at <= now),
                (Banner.ends_at.is_(None)) | (Banner.ends_at >= now),
            )
            .order_by(Banner.position, Banner.id)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(banner, translation) for banner, translation in rows]

    # ------------------------------------------------------------------ #
    # Admin reads
    # ------------------------------------------------------------------ #
    async def list_all(self) -> list[Banner]:
        """Return every banner with its translations, in display order.

        Returns:
            list[Banner]: Ordered by ``(position, id)``.
        """
        stmt = (
            select(Banner)
            .options(selectinload(Banner.translations))
            .order_by(Banner.position, Banner.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(self, banner_id: int) -> Banner | None:
        """Return one banner with its translations, or ``None``.

        Args:
            banner_id: Banner primary key.

        Returns:
            Banner | None: The banner, or ``None`` when it does not exist.
        """
        stmt = (
            select(Banner)
            .options(selectinload(Banner.translations))
            .where(Banner.id == banner_id)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def get_many(self, banner_ids: list[int]) -> list[Banner]:
        """Return the banners with these ids (for a bulk reorder).

        Args:
            banner_ids: Ids to load.

        Returns:
            list[Banner]: Found banners; missing ids are simply absent.
        """
        if not banner_ids:
            return []
        stmt = select(Banner).where(Banner.id.in_(banner_ids))
        return list((await self.session.execute(stmt)).scalars().all())

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    async def add(self, banner: Banner) -> Banner:
        """Persist a new banner and flush so its id is available.

        Args:
            banner: Transient banner with its translations attached.

        Returns:
            Banner: The same instance, now with an id.
        """
        self.session.add(banner)
        await self.session.flush()
        return banner

    async def clear_translations(self, banner_id: int) -> None:
        """Drop every translation of a banner (a full update replaces them).

        Args:
            banner_id: Banner whose translations to remove.
        """
        await self.session.execute(
            delete(BannerTranslation).where(BannerTranslation.banner_id == banner_id)
        )

    async def delete(self, banner: Banner) -> None:
        """Delete a banner; its translations cascade.

        Args:
            banner: The banner to remove.
        """
        await self.session.delete(banner)
