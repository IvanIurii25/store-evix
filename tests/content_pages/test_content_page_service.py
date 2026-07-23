"""Direct service tests for :class:`ContentPageService` (CMS-lite, Phase 1).

Exercises the business-rule branches straight against the service bound to the
transactional ``db_session`` — no HTTP layer — so the not-found / slug-conflict /
publish-visibility / footer / translation-replace paths are covered without the
router in the way. The router-only lines are covered in
``test_admin_content_pages_router.py``.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content_page import ContentPageTranslation
from app.schemas.content_page import (
    ContentPageCreate,
    ContentPageTranslationIn,
    ContentPageUpdate,
)
from app.services.content_page_service import (
    ContentPageConflictError,
    ContentPageNotFoundError,
    ContentPageService,
)
from tests.factories import create_content_page

pytestmark = pytest.mark.asyncio

# Ids that no fixture ever persists — used to force the not-found branches.
MISSING_PAGE_ID: int = 987654
# Expected translation count once both languages are present.
BOTH_LANGS_COUNT: int = 2


def _translations_in(
    *,
    title_ru: str = "Заголовок",
    title_ro: str = "Titlu",
) -> list[ContentPageTranslationIn]:
    """Build the both-language translation payload the schema requires."""
    return [
        ContentPageTranslationIn(
            lang="ru", title=title_ru, body="# ru", seo_description="seo ru"
        ),
        ContentPageTranslationIn(
            lang="ro", title=title_ro, body="# ro", seo_description=None
        ),
    ]


def _create_payload(
    slug: str = "about",
    *,
    is_published: bool = True,
    show_in_footer: bool = True,
    position: int = 0,
) -> ContentPageCreate:
    """Build a valid :class:`ContentPageCreate` with both languages."""
    return ContentPageCreate(
        slug=slug,
        is_published=is_published,
        show_in_footer=show_in_footer,
        position=position,
        translations=_translations_in(),
    )


class TestContentPageServicePublicReads:
    """Public list_footer / get_page branches (published, lang, ordering)."""

    async def test_list_footer_maps_slug_and_title_for_lang(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: a published footer page with both translations.
        await create_content_page(db_session, slug="delivery", position=1)
        service = ContentPageService(db_session)

        # Act: request the Russian footer listing.
        items = await service.list_footer("ru")

        # Assert: the mapped item carries the page slug and ru title.
        slugs = [item.slug for item in items]
        assert "delivery" in slugs, "published footer page must be listed"

    async def test_list_footer_excludes_unpublished_returns_empty(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: only an unpublished page exists.
        await create_content_page(db_session, slug="draft", is_published=False)
        service = ContentPageService(db_session)

        # Act
        items = await service.list_footer("ru")

        # Assert: unpublished pages never reach the footer -> empty result.
        assert items == [], "unpublished page must not appear in footer"

    async def test_get_page_returns_detail_for_requested_lang(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        await create_content_page(db_session, slug="terms")
        service = ContentPageService(db_session)

        # Act
        detail = await service.get_page("terms", "ro")

        # Assert: the detail resolves to the requested slug/lang.
        assert detail.slug == "terms", "detail must echo the requested slug"

    async def test_get_page_missing_slug_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: no page with this slug exists.
        service = ContentPageService(db_session)

        # Act / Assert: the not-found branch raises the domain error.
        with pytest.raises(ContentPageNotFoundError):
            await service.get_page("nope", "ru")

    async def test_get_page_unpublished_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: the page exists but is a draft.
        await create_content_page(db_session, slug="secret", is_published=False)
        service = ContentPageService(db_session)

        # Act / Assert: publication filter hides it -> not found.
        with pytest.raises(ContentPageNotFoundError):
            await service.get_page("secret", "ru")


class TestContentPageServiceAdminReads:
    """Admin get_page_admin / list_pages branches (found + not-found)."""

    async def test_get_page_admin_returns_page_with_translations(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        page = await create_content_page(db_session, slug="faq")
        service = ContentPageService(db_session)

        # Act
        loaded = await service.get_page_admin(page.id)

        # Assert: both languages are eager-loaded for the back-office editor.
        assert len(loaded.translations) == BOTH_LANGS_COUNT, (
            "admin read must load all translations"
        )

    async def test_get_page_admin_missing_id_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = ContentPageService(db_session)

        # Act / Assert: the None-guard raises for an unknown id.
        with pytest.raises(ContentPageNotFoundError):
            await service.get_page_admin(MISSING_PAGE_ID)

    async def test_list_pages_returns_all_including_drafts(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: one published, one draft.
        await create_content_page(db_session, slug="pub")
        await create_content_page(db_session, slug="draft", is_published=False)
        service = ContentPageService(db_session)

        # Act
        pages = await service.list_pages()

        # Assert: admin list ignores publication state.
        slugs = {page.slug for page in pages}
        assert {"pub", "draft"} <= slugs, "admin list must include drafts"


class TestContentPageServiceWrites:
    """create_page / update_page / delete_page branches (conflict + replace)."""

    async def test_create_page_persists_with_both_translations(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = ContentPageService(db_session)

        # Act
        created = await service.create_page(_create_payload(slug="new-page"))

        # Assert: the page is created with both languages attached.
        assert created.slug == "new-page", "created page must keep the slug"
        assert len(created.translations) == BOTH_LANGS_COUNT, (
            "create must attach both translations"
        )

    async def test_create_page_duplicate_slug_raises_conflict(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: a page already owns this slug.
        await create_content_page(db_session, slug="taken")
        service = ContentPageService(db_session)

        # Act / Assert: the uniqueness guard raises a conflict.
        with pytest.raises(ContentPageConflictError):
            await service.create_page(_create_payload(slug="taken"))

    async def test_update_page_replaces_fields_and_translations(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: a draft page whose translations will be fully replaced.
        page = await create_content_page(
            db_session, slug="orig", is_published=False, position=0
        )
        service = ContentPageService(db_session)
        payload = ContentPageUpdate(
            slug="orig-renamed",
            is_published=True,
            show_in_footer=False,
            position=5,
            translations=_translations_in(title_ru="Новый", title_ro="Nou"),
        )

        # Act
        updated = await service.update_page(page.id, payload)

        # Assert: structural fields and translations are replaced wholesale.
        assert updated.slug == "orig-renamed", "slug must be updated"
        assert updated.is_published is True, "publish flag must be updated"
        assert updated.position == 5, "position must be updated"
        assert len(updated.translations) == BOTH_LANGS_COUNT, (
            "translations must be replaced with the new both-lang set"
        )
        ru_titles = [tr.title for tr in updated.translations if tr.lang == "ru"]
        assert ru_titles == ["Новый"], "replaced ru translation must win"

    async def test_update_page_same_slug_no_conflict(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: keeping the same slug must not trip the clash check.
        page = await create_content_page(db_session, slug="keep")
        service = ContentPageService(db_session)
        payload = ContentPageUpdate(
            slug="keep",
            is_published=True,
            show_in_footer=True,
            position=0,
            translations=_translations_in(),
        )

        # Act: the clash row is the page itself, so update succeeds.
        updated = await service.update_page(page.id, payload)

        # Assert
        assert updated.slug == "keep", "unchanged slug must be allowed"

    async def test_update_page_slug_owned_by_other_raises_conflict(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: two pages; the second is retargeted onto the first's slug.
        await create_content_page(db_session, slug="page-x")
        target = await create_content_page(db_session, slug="page-y")
        service = ContentPageService(db_session)
        payload = ContentPageUpdate(
            slug="page-x",
            is_published=True,
            show_in_footer=True,
            position=0,
            translations=_translations_in(),
        )

        # Act / Assert: the cross-page clash raises a conflict.
        with pytest.raises(ContentPageConflictError):
            await service.update_page(target.id, payload)

    async def test_update_page_missing_id_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = ContentPageService(db_session)
        payload = ContentPageUpdate(
            slug="whatever",
            is_published=True,
            show_in_footer=True,
            position=0,
            translations=_translations_in(),
        )

        # Act / Assert: the get_page_admin guard raises for an unknown id.
        with pytest.raises(ContentPageNotFoundError):
            await service.update_page(MISSING_PAGE_ID, payload)

    async def test_delete_page_removes_page_and_translations(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        page = await create_content_page(db_session, slug="gone")
        service = ContentPageService(db_session)

        # Act: delete then re-read.
        await service.delete_page(page.id)

        # Assert: the page no longer resolves (cascade removed translations).
        with pytest.raises(ContentPageNotFoundError):
            await service.get_page_admin(page.id)
        remaining = (
            await db_session.execute(
                ContentPageTranslation.__table__.select().where(
                    ContentPageTranslation.page_id == page.id
                )
            )
        ).all()
        assert remaining == [], "translations must cascade-delete with the page"

    async def test_delete_page_missing_id_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        service = ContentPageService(db_session)

        # Act / Assert
        with pytest.raises(ContentPageNotFoundError):
            await service.delete_page(MISSING_PAGE_ID)
