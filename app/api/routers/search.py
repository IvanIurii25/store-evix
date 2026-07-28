"""Search + facets endpoints (§4 ``/search``, §6.2 category facets).

Thin router: it validates input, resolves the language, delegates to
:class:`~app.services.search_service.SearchService`, and serializes the result.
No business logic and no SQL live here. Domain errors raised by the service
subclass :class:`~app.core.errors.DomainError` and are rendered by the app's
registered handler into the unified ``{error:{code,message,details?}}`` envelope.

Mounted without the ``/api/v1`` prefix — the integrator adds it.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.lang import get_lang
from app.core.db import get_session
from app.schemas.search import FacetsResponse, SearchResponse
from app.services.search_service import (
    DEFAULT_SEARCH_PAGE_SIZE,
    MAX_SEARCH_PAGE_SIZE,
    SearchService,
)

router = APIRouter(tags=["search"])


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str = Query(..., min_length=1, description="Full-text search query."),
    page: int = Query(default=1, ge=1, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_SEARCH_PAGE_SIZE,
        ge=1,
        le=MAX_SEARCH_PAGE_SIZE,
        description="Results per page.",
    ),
    lang: str = Depends(get_lang),
    session: AsyncSession = Depends(get_session),
) -> SearchResponse:
    """Full-text search over the catalog, ranked and page-paginated (§4).

    Args:
        q: Full-text query (matched on name/description via the language config,
            plus article code).
        page: 1-based page number.
        page_size: Results per page.
        lang: Resolved request language.
        session: Injected async DB session.

    Returns:
        SearchResponse: ``{data, total, page, page_size}`` ordered by relevance.
    """
    service = SearchService(session)
    return await service.search(query=q, lang=lang, page=page, page_size=page_size)


@router.get(
    "/catalog/categories/{slug}/facets",
    response_model=FacetsResponse,
)
async def category_facets(
    slug: str,
    lang: str = Depends(get_lang),
    session: AsyncSession = Depends(get_session),
) -> FacetsResponse:
    """Return live facets for a category subtree (§6.2).

    Args:
        slug: Localized category slug.
        lang: Resolved request language.
        session: Injected async DB session.

    Returns:
        FacetsResponse: Attribute values + counts + price bounds for the subtree.

    Raises:
        NotFoundError: 404 if the category is unknown in the language (rendered
            by the unified :class:`~app.core.errors.DomainError` handler).
    """
    service = SearchService(session)
    return await service.facets(category_slug=slug, lang=lang)
