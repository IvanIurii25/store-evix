"""Catalog read endpoints (§4).

Thin router: it validates input, resolves the language, delegates to
:class:`~app.services.catalog_service.CatalogService`, and serializes the result.
No business logic and no SQL live here. Domain errors from the service are mapped
to HTTP status codes (the app's HTTPException handler renders the unified
``{error:{code,message,details?}}`` envelope).

Mounted without the ``/api/v1`` prefix — the integrator adds it.
"""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.lang import get_lang
from app.core.db import get_session
from app.schemas.catalog import (
    CategoryDetail,
    CategoryNode,
    ProductDetail,
    ProductListing,
    ProductSort,
    SitemapData,
)
from app.services.catalog_service import (
    CatalogService,
    InvalidCursorError,
    NotFoundError,
)

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("/sitemap", response_model=SitemapData)
async def get_sitemap(
    session: AsyncSession = Depends(get_session),
) -> SitemapData:
    """Flat feed of all published categories + products with per-language slugs."""
    return await CatalogService(session).get_sitemap()


@router.get("/categories", response_model=list[CategoryNode])
async def list_categories(
    lang: str = Depends(get_lang),
    session: AsyncSession = Depends(get_session),
) -> list[CategoryNode]:
    """Return the active category tree for the resolved language.

    Args:
        lang: Resolved request language.
        session: Injected async DB session.

    Returns:
        list[CategoryNode]: Root nodes with nested children.
    """
    service = CatalogService(session)
    return await service.list_categories(lang)


@router.get("/categories/{slug}", response_model=CategoryDetail)
async def get_category(
    slug: str,
    lang: str = Depends(get_lang),
    session: AsyncSession = Depends(get_session),
) -> CategoryDetail:
    """Return a category with breadcrumbs and subcategories.

    Args:
        slug: Localized category slug.
        lang: Resolved request language.
        session: Injected async DB session.

    Returns:
        CategoryDetail: The category detail response.

    Raises:
        HTTPException: 404 if the category is not found in the language.
    """
    service = CatalogService(session)
    try:
        return await service.get_category(slug, lang)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@router.get("/categories/{slug}/products", response_model=ProductListing)
async def list_category_products(
    slug: str,
    sort: ProductSort = Query(default=ProductSort.NEWEST),
    cursor: str | None = Query(default=None),
    price_min: Decimal | None = Query(default=None, ge=0),
    price_max: Decimal | None = Query(default=None, ge=0),
    value_ids: list[int] | None = Query(default=None),
    lang: str = Depends(get_lang),
    session: AsyncSession = Depends(get_session),
) -> ProductListing:
    """Return a keyset-paginated product listing for a category (§5.3).

    Args:
        slug: Localized category slug.
        sort: Sort order.
        cursor: Opaque cursor from a previous page (``None`` = first page).
        price_min: Optional inclusive lower price bound.
        price_max: Optional inclusive upper price bound.
        lang: Resolved request language.
        session: Injected async DB session.

    Returns:
        ProductListing: ``{data, next_cursor}`` envelope.

    Raises:
        HTTPException: 404 if the category is unknown, 400 for a bad cursor.
    """
    service = CatalogService(session)
    try:
        return await service.list_products(
            category_slug=slug,
            lang=lang,
            sort=sort.value,
            cursor=cursor,
            price_min=price_min,
            price_max=price_max,
            value_ids=value_ids,
        )
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except InvalidCursorError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.get("/products", response_model=ProductListing)
async def list_all_products(
    sort: ProductSort = Query(default=ProductSort.NEWEST),
    on_sale: bool = Query(default=False),
    cursor: str | None = Query(default=None),
    price_min: Decimal | None = Query(default=None, ge=0),
    price_max: Decimal | None = Query(default=None, ge=0),
    lang: str = Depends(get_lang),
    session: AsyncSession = Depends(get_session),
) -> ProductListing:
    """Return a keyset-paginated product listing across the whole store.

    Powers the homepage store-wide rails ("newest", "on sale"). Same read-model
    and cursor contract as the category listing, without a category constraint.

    Args:
        sort: Sort order.
        on_sale: Keep only products with a struck-through ``old_price``.
        cursor: Opaque cursor from a previous page (``None`` = first page).
        price_min: Optional inclusive lower price bound.
        price_max: Optional inclusive upper price bound.
        lang: Resolved request language.
        session: Injected async DB session.

    Returns:
        ProductListing: ``{data, next_cursor}`` envelope.

    Raises:
        HTTPException: 400 for a bad cursor.
    """
    service = CatalogService(session)
    try:
        return await service.list_all_products(
            lang=lang,
            sort=sort.value,
            cursor=cursor,
            price_min=price_min,
            price_max=price_max,
            on_sale=on_sale,
        )
    except InvalidCursorError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.get("/products/{slug}", response_model=ProductDetail)
async def get_product(
    slug: str,
    lang: str = Depends(get_lang),
    session: AsyncSession = Depends(get_session),
) -> ProductDetail:
    """Return a product detail page (characteristics, media, both slugs).

    Args:
        slug: Localized product slug.
        lang: Resolved request language.
        session: Injected async DB session.

    Returns:
        ProductDetail: The PDP response.

    Raises:
        HTTPException: 404 if the product is not found / inactive.
    """
    service = CatalogService(session)
    try:
        return await service.get_product(slug, lang)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
