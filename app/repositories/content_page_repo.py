"""Async data-access layer for content pages (CMS-lite, Phase 1).

Only SQL / query construction lives here — no business rules (those belong to
:class:`~app.services.content_page_service.ContentPageService`) and no HTTP.
Committing is the caller's (service's) responsibility.

Public reads return the page paired with the requested-language translation;
admin reads eager-load *all* translations (``selectinload``) so the back-office
can edit both languages without an N+1.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.content_page import ContentPage, ContentPageTranslation


class ContentPageRepository:
    """Read/write queries for content pages, bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used by every query method.

        Args:
            session: Active async session (request- or test-scoped).
        """
        self.session = session

    # ------------------------------------------------------------------ #
    # Public reads
    # ------------------------------------------------------------------ #
    async def list_published_footer(
        self,
        lang: str,
    ) -> list[tuple[ContentPage, ContentPageTranslation]]:
        """Return published footer pages with their ``lang`` translation.

        Only pages that are published, flagged ``show_in_footer`` and that have a
        translation for ``lang`` are returned, ordered by ``(position, id)``.

        Args:
            lang: Requested language code.

        Returns:
            list[tuple[ContentPage, ContentPageTranslation]]: Ordered pairs.
        """
        stmt = (
            select(ContentPage, ContentPageTranslation)
            .join(
                ContentPageTranslation,
                ContentPageTranslation.page_id == ContentPage.id,
            )
            .where(
                ContentPage.is_published.is_(True),
                ContentPage.show_in_footer.is_(True),
                ContentPageTranslation.lang == lang,
            )
            .order_by(ContentPage.position, ContentPage.id)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(page, tr) for page, tr in rows]

    async def get_published_by_slug(
        self,
        slug: str,
        lang: str,
    ) -> tuple[ContentPage, ContentPageTranslation] | None:
        """Return a published page + its ``lang`` translation, or ``None``.

        Args:
            slug: Page slug.
            lang: Requested language code.

        Returns:
            tuple[ContentPage, ContentPageTranslation] | None: The pair when the
            page is published and the language exists, else ``None``.
        """
        stmt = (
            select(ContentPage, ContentPageTranslation)
            .join(
                ContentPageTranslation,
                ContentPageTranslation.page_id == ContentPage.id,
            )
            .where(
                ContentPage.slug == slug,
                ContentPage.is_published.is_(True),
                ContentPageTranslation.lang == lang,
            )
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        return row[0], row[1]

    # ------------------------------------------------------------------ #
    # Admin reads
    # ------------------------------------------------------------------ #
    async def list_all(self) -> list[ContentPage]:
        """Return every page with all translations eager-loaded (ordered).

        Returns:
            list[ContentPage]: Pages ordered by ``(position, id)``.
        """
        stmt = (
            select(ContentPage)
            .options(selectinload(ContentPage.translations))
            .order_by(ContentPage.position, ContentPage.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(self, page_id: int) -> ContentPage | None:
        """Return one page with its translations eager-loaded, or ``None``.

        Args:
            page_id: The page id.

        Returns:
            ContentPage | None: The page, or ``None`` when absent.
        """
        stmt = (
            select(ContentPage)
            .options(selectinload(ContentPage.translations))
            .where(ContentPage.id == page_id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> ContentPage | None:
        """Return a page by slug (no publication filter), or ``None``.

        Used for the create/update duplicate-slug conflict check.

        Args:
            slug: The slug to look up.

        Returns:
            ContentPage | None: The page, or ``None`` when the slug is free.
        """
        stmt = select(ContentPage).where(ContentPage.slug == slug)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ------------------------------------------------------------------ #
    # Admin writes (caller commits)
    # ------------------------------------------------------------------ #
    async def create(self, page: ContentPage) -> ContentPage:
        """Add a new page (with its translations) and flush.

        Args:
            page: A fully-built :class:`ContentPage` with translations attached.

        Returns:
            ContentPage: The persisted page (id populated after flush).
        """
        self.session.add(page)
        await self.session.flush()
        return page

    async def delete(self, page: ContentPage) -> None:
        """Delete a page (translations cascade via the FK).

        Args:
            page: The page to delete.
        """
        await self.session.delete(page)
        await self.session.flush()
