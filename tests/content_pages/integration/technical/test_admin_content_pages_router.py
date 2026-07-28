"""Router-layer tests for the admin content-pages API (CMS-lite, Phase 1).

The route handlers are ``async def`` functions with the auth + session already
resolved by ``Depends``; here they are called **directly** (staff user + the
transactional ``db_session`` passed in) so coverage traces the handler bodies —
the ASGI/anyio path used by ``staff_client`` executes the handlers but coverage
cannot trace inside that worker. The handlers no longer catch service errors:
the domain errors propagate and the registered handler renders them into the
unified envelope. A couple of end-to-end ``staff_client`` tests remain as an
integration smoke check.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.admin_content_pages import (
    create_page,
    delete_page,
    get_page,
    list_pages,
    update_page,
)
from app.schemas.content_page import (
    ContentPageCreate,
    ContentPageTranslationIn,
    ContentPageUpdate,
)
from app.services.content_page_service import (
    ContentPageConflictError,
    ContentPageNotFoundError,
)
from tests.factories import create_content_page, create_staff

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/admin/content-pages"

# Id that no fixture persists — forces the 404 branches.
MISSING_PAGE_ID: int = 424242
# HTTP status constants (no magic numbers).
HTTP_CREATED: int = 201
HTTP_NO_CONTENT: int = 204
HTTP_OK: int = 200
HTTP_NOT_FOUND: int = 404
HTTP_CONFLICT: int = 409
# Expected translation count once both languages are present.
BOTH_LANGS_COUNT: int = 2


def _translations_in(
    *,
    title_ru: str = "Ру",
    title_ro: str = "Ro",
) -> list[ContentPageTranslationIn]:
    """Build the both-language translation payload the schema requires."""
    return [
        ContentPageTranslationIn(lang="ru", title=title_ru, body="# ru"),
        ContentPageTranslationIn(lang="ro", title=title_ro, body="# ro"),
    ]


def _create(
    slug: str = "router-page", *, is_published: bool = True
) -> ContentPageCreate:
    """Build a valid :class:`ContentPageCreate`."""
    return ContentPageCreate(
        slug=slug,
        is_published=is_published,
        show_in_footer=True,
        position=0,
        translations=_translations_in(),
    )


def _update(slug: str, *, is_published: bool = True) -> ContentPageUpdate:
    """Build a valid :class:`ContentPageUpdate`."""
    return ContentPageUpdate(
        slug=slug,
        is_published=is_published,
        show_in_footer=True,
        position=0,
        translations=_translations_in(),
    )


def _api_payload(slug: str = "api-page", *, is_published: bool = True) -> dict:
    """Build a valid create/update JSON payload for the ``staff_client`` tests."""
    return {
        "slug": slug,
        "is_published": is_published,
        "show_in_footer": True,
        "position": 0,
        "translations": [
            {"lang": "ru", "title": "Ру", "body": "# ru", "seo_description": "s"},
            {"lang": "ro", "title": "Ro", "body": "# ro", "seo_description": None},
        ],
    }


class TestAdminContentPagesHandlersReads:
    """Direct calls: list handler + get handler (found + not-found mapping)."""

    async def test_list_pages_returns_out_dtos(self, db_session: AsyncSession) -> None:
        # Arrange: two persisted pages + a staff user for the handler signature.
        staff = await create_staff(db_session)
        await create_content_page(db_session, slug="list-a")
        await create_content_page(db_session, slug="list-b")

        # Act: call the list handler directly.
        result = await list_pages(_staff=staff, session=db_session)

        # Assert: the handler maps each page into an admin DTO.
        slugs = {page.slug for page in result}
        assert {"list-a", "list-b"} <= slugs, "both pages must be returned"

    async def test_get_page_returns_out_dto(self, db_session: AsyncSession) -> None:
        # Arrange
        staff = await create_staff(db_session)
        page = await create_content_page(db_session, slug="detail")

        # Act
        result = await get_page(page_id=page.id, _staff=staff, session=db_session)

        # Assert: the handler returns the page DTO with both translations.
        assert len(result.translations) == BOTH_LANGS_COUNT, (
            "detail handler must include both translations"
        )

    async def test_get_page_missing_raises_404(self, db_session: AsyncSession) -> None:
        # Arrange
        staff = await create_staff(db_session)

        # Act / Assert: the handler propagates the not-found domain error.
        with pytest.raises(ContentPageNotFoundError) as caught:
            await get_page(page_id=MISSING_PAGE_ID, _staff=staff, session=db_session)
        assert caught.value.status_code == HTTP_NOT_FOUND, "renders as 404"
        assert caught.value.code == "not_found", "renders with the not_found code"


class TestAdminContentPagesHandlersWrites:
    """Direct calls: create/update/delete handlers + their error mappings."""

    async def test_create_page_returns_out_dto(self, db_session: AsyncSession) -> None:
        # Arrange
        staff = await create_staff(db_session)

        # Act
        result = await create_page(
            payload=_create(slug="fresh"), _staff=staff, session=db_session
        )

        # Assert: the create handler returns the persisted page DTO.
        assert result.slug == "fresh", "created DTO must carry the slug"

    async def test_create_duplicate_slug_raises_409(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: a page already owns the slug.
        staff = await create_staff(db_session)
        await create_content_page(db_session, slug="dup")

        # Act / Assert: the handler propagates the conflict domain error.
        with pytest.raises(ContentPageConflictError) as caught:
            await create_page(
                payload=_create(slug="dup"), _staff=staff, session=db_session
            )
        assert caught.value.status_code == HTTP_CONFLICT, "renders as 409"
        assert caught.value.code == "conflict", "renders with the conflict code"

    async def test_update_page_returns_out_dto(self, db_session: AsyncSession) -> None:
        # Arrange: a draft page to publish via update.
        staff = await create_staff(db_session)
        page = await create_content_page(db_session, slug="upd", is_published=False)

        # Act
        result = await update_page(
            page_id=page.id,
            payload=_update("upd", is_published=True),
            _staff=staff,
            session=db_session,
        )

        # Assert
        assert result.is_published is True, "update handler must persist publish"

    async def test_update_missing_raises_404(self, db_session: AsyncSession) -> None:
        # Arrange
        staff = await create_staff(db_session)

        # Act / Assert
        with pytest.raises(ContentPageNotFoundError) as caught:
            await update_page(
                page_id=MISSING_PAGE_ID,
                payload=_update("ghost"),
                _staff=staff,
                session=db_session,
            )
        assert caught.value.status_code == HTTP_NOT_FOUND, "renders as 404"

    async def test_update_slug_clash_raises_409(self, db_session: AsyncSession) -> None:
        # Arrange: retarget page-b onto page-a's slug.
        staff = await create_staff(db_session)
        await create_content_page(db_session, slug="own-a")
        target = await create_content_page(db_session, slug="own-b")

        # Act / Assert
        with pytest.raises(ContentPageConflictError) as caught:
            await update_page(
                page_id=target.id,
                payload=_update("own-a"),
                _staff=staff,
                session=db_session,
            )
        assert caught.value.status_code == HTTP_CONFLICT, "renders as 409"

    async def test_delete_page_succeeds(self, db_session: AsyncSession) -> None:
        # Arrange
        staff = await create_staff(db_session)
        page = await create_content_page(db_session, slug="rm")

        # Act: the delete handler returns None on success.
        result = await delete_page(page_id=page.id, _staff=staff, session=db_session)

        # Assert
        assert result is None, "delete handler must return None on success"

    async def test_delete_missing_raises_404(self, db_session: AsyncSession) -> None:
        # Arrange
        staff = await create_staff(db_session)

        # Act / Assert
        with pytest.raises(ContentPageNotFoundError) as caught:
            await delete_page(page_id=MISSING_PAGE_ID, _staff=staff, session=db_session)
        assert caught.value.status_code == HTTP_NOT_FOUND, "renders as 404"


class TestAdminContentPagesApiSmoke:
    """End-to-end smoke over ``staff_client`` (real ASGI stack + error envelope)."""

    async def test_create_then_list_via_client(self, staff_client: AsyncClient) -> None:
        # Arrange / Act: create a page then list.
        created = await staff_client.post(_BASE, json=_api_payload(slug="smoke"))
        assert created.status_code == HTTP_CREATED, created.text
        listing = await staff_client.get(_BASE)

        # Assert: the created page is present in the listing.
        assert listing.status_code == HTTP_OK, listing.text
        slugs = {item["slug"] for item in listing.json()}
        assert "smoke" in slugs, "created page must appear in the listing"

    async def test_missing_page_error_envelope(self, staff_client: AsyncClient) -> None:
        # Act: fetch an unknown id through the real stack.
        resp = await staff_client.get(f"{_BASE}/{MISSING_PAGE_ID}")

        # Assert: the domain error surfaces as the unified 404 envelope.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found", "envelope code check"
