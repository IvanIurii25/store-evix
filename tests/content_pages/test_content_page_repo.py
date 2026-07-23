"""Direct repository tests for :class:`ContentPageRepository` (CMS-lite, Phase 1).

Covers the query methods straight against the transactional ``db_session`` — the
footer/slug reads (hit + empty), the admin eager-loads, the slug lookup, and the
create/flush write — without the service or router in the way.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content_page import ContentPage, ContentPageTranslation
from app.repositories.content_page_repo import ContentPageRepository
from tests.factories import create_content_page

pytestmark = pytest.mark.asyncio

# Slug/id that no fixture ever persists — forces the None branches.
MISSING_SLUG: str = "does-not-exist"
MISSING_PAGE_ID: int = 555111
# Expected translation count once both languages are present.
BOTH_LANGS_COUNT: int = 2


class TestContentPageRepoPublicReads:
    """list_published_footer / get_published_by_slug (hit + miss)."""

    async def test_list_published_footer_returns_ordered_pairs(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: two footer pages with distinct positions.
        await create_content_page(db_session, slug="second", position=2)
        await create_content_page(db_session, slug="first", position=1)
        repo = ContentPageRepository(db_session)

        # Act
        rows = await repo.list_published_footer("ru")

        # Assert: pairs are returned ordered by position.
        slugs = [page.slug for page, _tr in rows]
        assert slugs == ["first", "second"], "footer must be ordered by position"

    async def test_list_published_footer_empty_when_none_match(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: a page hidden from the footer.
        await create_content_page(db_session, slug="hidden", show_in_footer=False)
        repo = ContentPageRepository(db_session)

        # Act
        rows = await repo.list_published_footer("ru")

        # Assert: no matching row -> empty list (return-comprehension over []).
        assert rows == [], "non-footer page must yield an empty footer"

    async def test_get_published_by_slug_returns_pair(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        await create_content_page(db_session, slug="terms")
        repo = ContentPageRepository(db_session)

        # Act
        found = await repo.get_published_by_slug("terms", "ru")

        # Assert: a (page, translation) pair is returned for a live page.
        assert found is not None, "published page must resolve"
        page, tr = found
        assert page.slug == "terms", "returned page must match the slug"
        assert tr.lang == "ru", "returned translation must match the lang"

    async def test_get_published_by_slug_missing_returns_none(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        repo = ContentPageRepository(db_session)

        # Act
        found = await repo.get_published_by_slug(MISSING_SLUG, "ru")

        # Assert: the empty-row branch returns None.
        assert found is None, "absent slug must return None"

    async def test_get_published_by_slug_unpublished_returns_none(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: a draft page must not resolve publicly.
        await create_content_page(db_session, slug="draft", is_published=False)
        repo = ContentPageRepository(db_session)

        # Act
        found = await repo.get_published_by_slug("draft", "ru")

        # Assert
        assert found is None, "unpublished page must return None"


class TestContentPageRepoAdminReads:
    """list_all / get / get_by_slug (eager-load + lookups)."""

    async def test_list_all_eager_loads_translations(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        await create_content_page(db_session, slug="one")
        repo = ContentPageRepository(db_session)

        # Act
        pages = await repo.list_all()

        # Assert: every page carries its eager-loaded translations.
        assert pages, "list_all must return persisted pages"
        assert len(pages[0].translations) == BOTH_LANGS_COUNT, (
            "translations must be eager-loaded"
        )

    async def test_get_returns_page_when_present(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        page = await create_content_page(db_session, slug="lookup")
        repo = ContentPageRepository(db_session)

        # Act
        found = await repo.get(page.id)

        # Assert
        assert found is not None and found.id == page.id, "get must find the page"

    async def test_get_missing_id_returns_none(self, db_session: AsyncSession) -> None:
        # Arrange
        repo = ContentPageRepository(db_session)

        # Act
        found = await repo.get(MISSING_PAGE_ID)

        # Assert
        assert found is None, "unknown id must return None"

    async def test_get_by_slug_ignores_publication_state(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: an unpublished page is still found by slug (conflict check).
        await create_content_page(db_session, slug="draft-slug", is_published=False)
        repo = ContentPageRepository(db_session)

        # Act
        found = await repo.get_by_slug("draft-slug")

        # Assert
        assert found is not None, "get_by_slug must ignore publication filter"

    async def test_get_by_slug_missing_returns_none(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange
        repo = ContentPageRepository(db_session)

        # Act
        found = await repo.get_by_slug(MISSING_SLUG)

        # Assert
        assert found is None, "free slug must return None"


class TestContentPageRepoWrites:
    """create (add + flush) / delete."""

    async def test_create_flushes_and_assigns_id(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: a fully-built page with both translations attached.
        page = ContentPage(
            slug="fresh",
            is_published=True,
            show_in_footer=True,
            position=0,
            translations=[
                ContentPageTranslation(lang="ru", title="Ру", body="# ru"),
                ContentPageTranslation(lang="ro", title="Ro", body="# ro"),
            ],
        )
        repo = ContentPageRepository(db_session)

        # Act
        created = await repo.create(page)

        # Assert: the flush populated the primary key.
        assert created.id is not None, "create must flush and assign an id"

    async def test_delete_removes_page(self, db_session: AsyncSession) -> None:
        # Arrange
        page = await create_content_page(db_session, slug="del")
        repo = ContentPageRepository(db_session)

        # Act
        await repo.delete(page)

        # Assert
        assert await repo.get(page.id) is None, "deleted page must not resolve"
