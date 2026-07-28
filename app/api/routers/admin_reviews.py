"""Admin reviews moderation router (Reviews & Ratings, Phase 1, §4).

Thin back-office router, entirely behind ``Depends(current_staff)`` (a non-staff
caller gets 403 from the dependency, a guest 401). It delegates to
:class:`~app.services.review_service.ReviewService`; domain failures raise
:class:`~app.services.review_service.ReviewError` subclasses that the shared
handler renders into the unified envelope. No business logic and no SQL live here.

Endpoints (paths carry ``/admin/reviews``; the ``/api/v1`` prefix is added by the
integrator when mounting):

* ``GET    /admin/reviews?status=&product_id=&cursor=`` — moderation queue.
* ``GET    /admin/reviews/pending-count``               — nav badge count.
* ``PATCH  /admin/reviews/{review_id}`` ``{status}``    — approve / reject.
* ``DELETE /admin/reviews/{review_id}``                 — delete.
"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.core.db import get_session
from app.schemas.review import (
    AdminReviewList,
    AdminReviewOut,
    PendingCountOut,
    ReviewModerateIn,
)
from app.services.review_service import ReviewService

router = APIRouter(
    prefix="/admin/reviews",
    tags=["admin-reviews"],
    dependencies=[Depends(current_staff)],
)


def get_review_service(
    session: AsyncSession = Depends(get_session),
) -> ReviewService:
    """Construct the review service bound to the request session."""
    return ReviewService(session=session)


@router.get("", response_model=AdminReviewList)
async def list_reviews(
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="Exact review-status filter (pending/approved/rejected).",
    ),
    product_id: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    service: ReviewService = Depends(get_review_service),
) -> AdminReviewList:
    """Return a keyset-paginated moderation-queue page (§4).

    Args:
        status_filter: Optional exact status filter (``status`` query param).
        product_id: Optional exact product filter.
        cursor: Opaque cursor from a previous page (``None`` = first page).
        service: Injected review service.

    Returns:
        AdminReviewList: ``{data, next_cursor}`` envelope.

    Raises:
        InvalidCursorError: 422 for a malformed cursor.
    """
    return await service.list_admin(
        status=status_filter,
        product_id=product_id,
        cursor=cursor,
    )


@router.get("/pending-count", response_model=PendingCountOut)
async def pending_count(
    service: ReviewService = Depends(get_review_service),
) -> PendingCountOut:
    """Return the number of reviews awaiting moderation (nav badge, §4).

    Args:
        service: Injected review service.

    Returns:
        PendingCountOut: ``{count}``.
    """
    return PendingCountOut(count=await service.pending_count())


@router.patch("/{review_id}", response_model=AdminReviewOut)
async def moderate_review(
    review_id: int,
    payload: ReviewModerateIn,
    service: ReviewService = Depends(get_review_service),
) -> AdminReviewOut:
    """Approve or reject a review, stamping ``moderated_at`` (§4).

    Args:
        review_id: The review to moderate.
        payload: The target status (``approved`` / ``rejected``).
        service: Injected review service.

    Returns:
        AdminReviewOut: The updated review.

    Raises:
        ReviewNotFoundError: 404 if the review does not exist.
    """
    review = await service.moderate(review_id, payload.status)
    return AdminReviewOut.model_validate(review)


@router.delete("/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_review(
    review_id: int,
    service: ReviewService = Depends(get_review_service),
) -> None:
    """Hard-delete any review (§4).

    Args:
        review_id: The review to delete.
        service: Injected review service.

    Raises:
        ReviewNotFoundError: 404 if the review does not exist.
    """
    await service.delete_admin(review_id)
