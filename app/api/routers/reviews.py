"""Customer reviews router (Reviews & Ratings, Phase 1, §4).

Thin router over :class:`~app.services.review_service.ReviewService`. The write /
"mine" endpoints require an authenticated user
(:func:`~app.api.deps.current_user` → 401 for guests; the storefront reads that
401 as "guest" and redirects to login with a post-login intent). The public
product-reviews list is open to everyone and lives under the catalog path so the
PDP can fetch it by slug.

* ``POST   /reviews``                              — submit / edit own review (→ pending).
* ``GET    /reviews/mine/{product_id}``            — the caller's own review, any status.
* ``DELETE /reviews/{review_id}``                  — delete own review (author only).
* ``GET    /catalog/products/{slug}/reviews``      — public approved list + aggregate.

No business logic and no SQL live here. Mounted without the ``/api/v1`` prefix —
the integrator adds it.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.api.lang import get_lang
from app.core.db import get_session
from app.models.user import AppUser
from app.repositories.catalog_repo import CatalogRepository
from app.schemas.review import (
    ProductReviewsOut,
    ReviewOut,
    ReviewSort,
    ReviewSubmitIn,
)
from app.services.review_service import ReviewService

router = APIRouter(tags=["reviews"])


def get_review_service(
    session: AsyncSession = Depends(get_session),
) -> ReviewService:
    """Construct the review service bound to the request session."""
    return ReviewService(session=session)


@router.post("/reviews", response_model=ReviewOut)
async def submit_review(
    payload: ReviewSubmitIn,
    user: AppUser = Depends(current_user),
    service: ReviewService = Depends(get_review_service),
) -> ReviewOut:
    """Create or update the current user's review of a product (→ pending, §4).

    Args:
        payload: Product id, rating (1..5) and optional title/body/name/lang.
        user: The authenticated user (401 for guests).
        service: Injected review service.

    Returns:
        ReviewOut: The persisted, pending review.

    Raises:
        ReviewNotFoundError: 404 if the product does not exist.
    """
    review = await service.submit(user.id, payload)
    return ReviewOut.model_validate(review)


@router.get("/reviews/mine/{product_id}", response_model=ReviewOut | None)
async def get_my_review(
    product_id: int,
    user: AppUser = Depends(current_user),
    service: ReviewService = Depends(get_review_service),
) -> ReviewOut | None:
    """Return the current user's own review of a product, any status (§4).

    Args:
        product_id: Product to look up.
        user: The authenticated user (401 for guests).
        service: Injected review service.

    Returns:
        ReviewOut | None: The user's review, or ``None`` if they have none.
    """
    return await service.get_mine(user.id, product_id)


@router.delete("/reviews/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_review(
    review_id: int,
    user: AppUser = Depends(current_user),
    service: ReviewService = Depends(get_review_service),
) -> None:
    """Delete the current user's own review (author only, §4).

    Args:
        review_id: The review to delete.
        user: The authenticated user (401 for guests).
        service: Injected review service.

    Raises:
        ReviewNotFoundError: 404 if the review is missing.
        ReviewForbiddenError: 403 if it is not the caller's own review.
    """
    await service.delete_own(user.id, review_id)


@router.get(
    "/catalog/products/{slug}/reviews",
    response_model=ProductReviewsOut,
)
async def list_product_reviews(
    slug: str,
    sort: ReviewSort = Query(default=ReviewSort.NEWEST),
    cursor: str | None = Query(default=None),
    lang: str = Depends(get_lang),
    session: AsyncSession = Depends(get_session),
) -> ProductReviewsOut:
    """Return a product's public approved reviews + aggregate, by slug (§4).

    Public (no auth). Resolves the localized slug to a product id, then returns
    the approved-review list (keyset-paginated) and the rating aggregate
    (avg / count / per-star distribution). An unknown slug yields 404.

    Args:
        slug: Localized product slug.
        sort: ``newest`` (default) / ``highest`` / ``lowest``.
        cursor: Opaque cursor from a previous page (``None`` = first page).
        lang: Resolved request language (for slug resolution).
        session: Injected async DB session.

    Returns:
        ProductReviewsOut: ``{aggregate, data, next_cursor}`` envelope.

    Raises:
        HTTPException: 404 if the slug is unknown.
        InvalidCursorError: 422 for a malformed or sort-mismatched cursor.
    """
    translation = await CatalogRepository(session).get_product_translation_by_slug(
        slug, lang
    )
    if translation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": f"Product '{slug}' not found"},
        )
    return await ReviewService(session).list_public(
        product_id=translation.product_id,
        sort=sort,
        cursor=cursor,
    )
