"""Public site-config router — read-only storefront settings (§6.4).

Exposes the site-wide SEO defaults the storefront needs to render page ``<title>``,
meta description and Open Graph tags on every SSR page. Unlike
``admin_settings_router`` (behind ``current_staff``), this endpoint is public and
read-only: it returns only the six non-sensitive SEO fields — no staff, no PII —
so an unauthenticated SSR render can fetch them. Writes stay admin-only.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.schemas.admin_settings import SeoSettings
from app.services.admin_settings_service import SettingsService

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
