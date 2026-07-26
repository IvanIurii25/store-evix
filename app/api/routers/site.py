"""Public site-config router — read-only storefront settings (§6.4).

Exposes the site-wide SEO defaults the storefront needs to render page ``<title>``,
meta description and Open Graph tags on every SSR page. Unlike
``admin_settings_router`` (behind ``current_staff``), this endpoint is public and
read-only: it returns only the six non-sensitive SEO fields — no staff, no PII —
so an unauthenticated SSR render can fetch them. Writes stay admin-only.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.schemas.admin_settings import SeoSettings
from app.schemas.content_page import ContentPageDetail, ContentPageListItem
from app.schemas.payment import SiteConfigOut
from app.services.admin_settings_service import SettingsService
from app.services.content_page_service import (
    ContentPageNotFoundError,
    ContentPageService,
)

# Default storefront language when the ``lang`` query param is omitted.
_DEFAULT_LANG: str = "ro"

router = APIRouter(prefix="/site", tags=["site"])


@router.get("/seo", response_model=SeoSettings)
async def get_site_seo(
    session: AsyncSession = Depends(get_session),
) -> SeoSettings:
    """Return the site-wide SEO defaults for the storefront (public, read-only).

    Args:
        session: Injected async DB session.

    Returns:
        SeoSettings: A fully-formed block (empty strings on a fresh install).
    """
    return await SettingsService(session).get_seo()


@router.get("/config", response_model=SiteConfigOut)
async def get_site_config() -> SiteConfigOut:
    """Return public storefront runtime config (feature flags), read-only.

    Currently exposes only ``card_payment_enabled`` so the checkout UI can decide
    whether to render the card option next to COD. No DB access, no PII.

    Returns:
        SiteConfigOut: The public config block.
    """
    return SiteConfigOut(card_payment_enabled=settings.card_payment_enabled)


@router.get("/pages", response_model=list[ContentPageListItem])
async def list_content_pages(
    lang: str = Query(default=_DEFAULT_LANG),
    session: AsyncSession = Depends(get_session),
) -> list[ContentPageListItem]:
    """Return published footer content pages for the storefront (public).

    Args:
        lang: Requested language code (default ``ro``).
        session: Injected async DB session.

    Returns:
        list[ContentPageListItem]: Ordered ``(slug, title)`` footer entries.
    """
    return await ContentPageService(session).list_footer(lang)


@router.get("/pages/{slug}", response_model=ContentPageDetail)
async def get_content_page(
    slug: str,
    lang: str = Query(default=_DEFAULT_LANG),
    session: AsyncSession = Depends(get_session),
) -> ContentPageDetail:
    """Return a single published content page rendered for ``lang`` (public).

    Args:
        slug: Page slug.
        lang: Requested language code (default ``ro``).
        session: Injected async DB session.

    Returns:
        ContentPageDetail: The rendered page.

    Raises:
        HTTPException: 404 if the page is not published or the language is absent.
    """
    try:
        return await ContentPageService(session).get_page(slug, lang)
    except ContentPageNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": str(exc)},
        ) from exc
