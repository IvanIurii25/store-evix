"""Content-page service: public reads + back-office CRUD (CMS-lite, Phase 1).

Wraps :class:`~app.repositories.content_page_repo.ContentPageRepository` with the
business rules and owns committing. No HTTP knowledge here — the raised domain
errors (:class:`ContentPageNotFoundError`, :class:`ContentPageConflictError`)
subclass :class:`~app.core.errors.DomainError` and are rendered into the unified
envelope by the registered handler.

The schema layer already guarantees both-language translations on create/update,
so the service focuses on slug-uniqueness and existence invariants.
"""

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.models.content_page import ContentPage, ContentPageTranslation
from app.repositories.content_page_repo import ContentPageRepository
from app.schemas.content_page import (
    ContentPageCreate,
    ContentPageDetail,
    ContentPageListItem,
    ContentPageUpdate,
)


class ContentPageError(DomainError):
    """Base class for content-page domain errors (rendered by the unified handler)."""

    code: str = "content_page_error"


class ContentPageNotFoundError(ContentPageError):
    """The referenced content page does not exist (renders as 404 ``not_found``)."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ContentPageConflictError(ContentPageError):
    """A write violates the slug-uniqueness invariant (renders as 409 ``conflict``)."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class ContentPageService:
    """Public reads and back-office CRUD for content pages."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and its repository.

        Args:
            session: Active async session (request- or test-scoped).
        """
        self.session = session
        self.repo = ContentPageRepository(session)

    # ------------------------------------------------------------------ #
    # Public
    # ------------------------------------------------------------------ #
    async def list_footer(self, lang: str) -> list[ContentPageListItem]:
        """Return published footer pages for ``lang`` (ordered by position).

        Args:
            lang: Requested language code.

        Returns:
            list[ContentPageListItem]: Compact ``(slug, title)`` entries.
        """
        rows = await self.repo.list_published_footer(lang)
        return [
            ContentPageListItem(slug=page.slug, title=tr.title) for page, tr in rows
        ]

    async def get_page(self, slug: str, lang: str) -> ContentPageDetail:
        """Return a published page rendered for ``lang``.

        Args:
            slug: Page slug.
            lang: Requested language code.

        Returns:
            ContentPageDetail: The rendered page.

        Raises:
            ContentPageNotFoundError: If the page is not published or has no
                translation for ``lang``.
        """
        found = await self.repo.get_published_by_slug(slug, lang)
        if found is None:
            raise ContentPageNotFoundError(f"Content page not found: {slug} ({lang})")
        page, tr = found
        return ContentPageDetail(
            slug=page.slug,
            title=tr.title,
            body=tr.body,
            seo_description=tr.seo_description,
        )

    # ------------------------------------------------------------------ #
    # Admin
    # ------------------------------------------------------------------ #
    async def list_pages(self) -> list[ContentPage]:
        """Return every page with all translations (back-office list)."""
        return await self.repo.list_all()

    async def get_page_admin(self, page_id: int) -> ContentPage:
        """Return one page with all translations (back-office detail).

        Args:
            page_id: The page id.

        Returns:
            ContentPage: The page with translations loaded.

        Raises:
            ContentPageNotFoundError: If the page does not exist.
        """
        page = await self.repo.get(page_id)
        if page is None:
            raise ContentPageNotFoundError(f"Content page not found: {page_id}")
        return page

    async def create_page(self, data: ContentPageCreate) -> ContentPage:
        """Create a content page with both-language translations.

        Args:
            data: Validated create payload (both langs guaranteed by the schema).

        Returns:
            ContentPage: The created page with translations loaded.

        Raises:
            ContentPageConflictError: If the slug is already taken.
        """
        if await self.repo.get_by_slug(data.slug) is not None:
            raise ContentPageConflictError(f"Slug already in use: {data.slug}")

        page = ContentPage(
            slug=data.slug,
            is_published=data.is_published,
            show_in_footer=data.show_in_footer,
            position=data.position,
            translations=[
                ContentPageTranslation(
                    lang=tr.lang,
                    title=tr.title,
                    body=tr.body,
                    seo_description=tr.seo_description,
                )
                for tr in data.translations
            ],
        )
        created = await self.repo.create(page)
        await self.session.commit()
        return await self.get_page_admin(created.id)

    async def update_page(
        self,
        page_id: int,
        data: ContentPageUpdate,
    ) -> ContentPage:
        """Fully update a page: structural fields + both-language translations.

        Args:
            page_id: The page to update.
            data: Validated update payload (both langs guaranteed by the schema).

        Returns:
            ContentPage: The updated page with translations loaded.

        Raises:
            ContentPageNotFoundError: If the page does not exist.
            ContentPageConflictError: If the new slug clashes with another page.
        """
        page = await self.get_page_admin(page_id)

        clash = await self.repo.get_by_slug(data.slug)
        if clash is not None and clash.id != page.id:
            raise ContentPageConflictError(f"Slug already in use: {data.slug}")

        page.slug = data.slug
        page.is_published = data.is_published
        page.show_in_footer = data.show_in_footer
        page.position = data.position
        # Full replace of translations (both langs are always present).
        page.translations.clear()
        for tr in data.translations:
            page.translations.append(
                ContentPageTranslation(
                    lang=tr.lang,
                    title=tr.title,
                    body=tr.body,
                    seo_description=tr.seo_description,
                )
            )
        await self.session.flush()
        await self.session.commit()
        return await self.get_page_admin(page.id)

    async def delete_page(self, page_id: int) -> None:
        """Delete a page and its translations (cascade).

        Args:
            page_id: The page to delete.

        Raises:
            ContentPageNotFoundError: If the page does not exist.
        """
        page = await self.get_page_admin(page_id)
        await self.repo.delete(page)
        await self.session.commit()
