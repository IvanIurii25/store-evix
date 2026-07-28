"""Admin content-pages API (CMS-lite CRUD, Phase 1).

Thin back-office router entirely behind :func:`~app.api.deps.current_staff`
(``is_staff`` → 401 for anonymous / 403 for non-staff). It validates input via
Pydantic and delegates to
:class:`~app.services.content_page_service.ContentPageService`; the service's
domain errors are rendered into the unified envelope by the registered handler.
No business logic and no SQL here.

Prefix is ``/admin/content-pages`` (the integrator mounts the router under
``/api/v1``).
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.core.db import get_session
from app.models.content_page import ContentPage
from app.models.user import AppUser
from app.schemas.content_page import (
    ContentPageAdminOut,
    ContentPageCreate,
    ContentPageUpdate,
)
from app.services.content_page_service import ContentPageService

router = APIRouter(
    prefix="/admin/content-pages",
    tags=["admin-content-pages"],
    dependencies=[Depends(current_staff)],
)


def _to_out(page: ContentPage) -> ContentPageAdminOut:
    """Build the admin response DTO from a loaded page."""
    return ContentPageAdminOut.model_validate(page)


@router.get("", response_model=list[ContentPageAdminOut])
async def list_pages(
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> list[ContentPageAdminOut]:
    """Return every content page with all translations (ordered by position)."""
    pages = await ContentPageService(session).list_pages()
    return [_to_out(page) for page in pages]


@router.post(
    "",
    response_model=ContentPageAdminOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_page(
    payload: ContentPageCreate,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> ContentPageAdminOut:
    """Create a content page with both-language translations (409 on dup slug)."""
    page = await ContentPageService(session).create_page(payload)
    return _to_out(page)


@router.get("/{page_id}", response_model=ContentPageAdminOut)
async def get_page(
    page_id: int,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> ContentPageAdminOut:
    """Return one content page with all translations (404 if absent)."""
    page = await ContentPageService(session).get_page_admin(page_id)
    return _to_out(page)


@router.put("/{page_id}", response_model=ContentPageAdminOut)
async def update_page(
    page_id: int,
    payload: ContentPageUpdate,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> ContentPageAdminOut:
    """Fully update a content page (404 if absent; 409 on slug clash)."""
    page = await ContentPageService(session).update_page(page_id, payload)
    return _to_out(page)


@router.delete("/{page_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_page(
    page_id: int,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a content page and its translations (404 if absent)."""
    await ContentPageService(session).delete_page(page_id)
