"""Reviews & Ratings business logic (Phase 1, §4 / §6).

Sits between the routers and :class:`ReviewRepository`. Owns the domain rules:

* You may only review a product that exists (:class:`ReviewNotFoundError` → 404).
* One review per ``(product, user)``: submit is an idempotent upsert; creating
  **or** editing sends the review back to ``pending`` for re-moderation (§6.1).
* ``is_verified`` is a snapshot taken at submit time from a non-cancelled order
  line (§2) — never recomputed on read.
* Only the author may delete their own review (:class:`ReviewForbiddenError`
  → 403); a missing review is 404.
* The public list returns only ``approved`` reviews plus an aggregate
  (avg / count / per-star distribution, §3), keyset-paginated by ``id``.
* Moderation sets ``moderated_at`` when a review is approved / rejected.

The cursor is an opaque base64 token carrying the sort key + last id, mirroring
the catalog listing cursor (§5.3) so a token can never be replayed across sorts.

No HTTP knowledge: the routers map the raised exceptions.
"""

import base64
import binascii
import json
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Product
from app.models.review import (
    MAX_RATING,
    STATUS_PENDING,
    Review,
)
from app.repositories.review_repo import ReviewRepository
from app.schemas.review import (
    AdminReviewList,
    AdminReviewOut,
    ProductReviewsOut,
    PublicReviewOut,
    RatingAggregate,
    ReviewOut,
    ReviewSort,
    ReviewSubmitIn,
)

# Public / admin list page sizes (§4).
PUBLIC_PAGE_SIZE: int = 10
ADMIN_PAGE_SIZE: int = 20


class ReviewError(Exception):
    """Base class for review domain errors (mapped to HTTP by the router)."""

    code: str = "review_error"


class ReviewNotFoundError(ReviewError):
    """The referenced product or review does not exist."""

    code = "not_found"


class ReviewForbiddenError(ReviewError):
    """The caller is not the author of the review they tried to modify."""

    code = "forbidden"


class InvalidCursorError(ReviewError):
    """The supplied review-list cursor is malformed or sort-mismatched."""

    code = "invalid_cursor"


def _encode_cursor(sort: str, last_id: int) -> str:
    """Serialize the keyset position into an opaque base64 token.

    Args:
        sort: Sort key the cursor belongs to.
        last_id: ``id`` of the last row on the page.

    Returns:
        str: URL-safe base64 cursor token.
    """
    payload = {"s": sort, "id": last_id}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str, sort: str) -> int:
    """Parse an opaque cursor and validate it against the current sort.

    Args:
        cursor: The token produced by :func:`_encode_cursor`.
        sort: The sort key of the current request.

    Returns:
        int: The last id from the previous page.

    Raises:
        InvalidCursorError: If the token is malformed or its sort mismatches.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw)
        cursor_sort = payload["s"]
        last_id = int(payload["id"])
    except (binascii.Error, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("Malformed review cursor") from exc
    if cursor_sort != sort:
        raise InvalidCursorError("Cursor does not match the requested sort order")
    return last_id


class ReviewService:
    """Review operations bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and its repository.

        Args:
            session: Active async session used for all reads and writes.
        """
        self.session = session
        self.repo = ReviewRepository(session)

    # ------------------------------------------------------------------ #
    # Customer path
    # ------------------------------------------------------------------ #
    async def submit(self, user_id: int, payload: ReviewSubmitIn) -> Review:
        """Create or update the caller's review of a product → ``pending`` (§4).

        Idempotent on ``(product_id, user_id)``: a first submission creates the
        review, a subsequent one edits it in place. Either way the status is set
        to ``pending`` (an edit re-enters moderation, §6.1) and ``moderated_at``
        is cleared. ``is_verified`` is snapshotted from a non-cancelled order
        line at this moment.

        Args:
            user_id: The authenticated author.
            payload: Validated submit body (rating 1..5 already enforced by the
                schema).

        Returns:
            Review: The persisted, pending review.

        Raises:
            ReviewNotFoundError: If the product does not exist.
        """
        product = await self.session.get(Product, payload.product_id)
        if product is None:
            raise ReviewNotFoundError(f"Product {payload.product_id} not found")

        is_verified = await self.repo.has_purchased(payload.product_id, user_id)
        review = await self.repo.get(payload.product_id, user_id)
        if review is None:
            review = Review(
                product_id=payload.product_id,
                user_id=user_id,
                rating=payload.rating,
                title=payload.title,
                body=payload.body,
                author_name=payload.author_name,
                status=STATUS_PENDING,
                is_verified=is_verified,
                lang=payload.lang,
            )
            self.session.add(review)
        else:
            review.rating = payload.rating
            review.title = payload.title
            review.body = payload.body
            review.author_name = payload.author_name
            review.status = STATUS_PENDING
            review.is_verified = is_verified
            review.lang = payload.lang
            review.moderated_at = None
        await self.session.flush()
        await self.session.commit()
        # ``created_at`` / ``updated_at`` are DB ``server_default`` columns not
        # populated on the instance after commit — refresh so the serialized
        # response carries them (avoids a lazy-load in the async serializer).
        await self.session.refresh(review)
        return review

    async def get_mine(self, user_id: int, product_id: int) -> ReviewOut | None:
        """Return the caller's own review of a product, any status (§4).

        Args:
            user_id: The authenticated user.
            product_id: Product to look up.

        Returns:
            ReviewOut | None: The user's review, or ``None`` if they have none.
        """
        review = await self.repo.get(product_id, user_id)
        if review is None:
            return None
        return ReviewOut.model_validate(review)

    async def delete_own(self, user_id: int, review_id: int) -> None:
        """Delete the caller's own review (author-only, §4).

        Args:
            user_id: The authenticated user.
            review_id: The review to delete.

        Raises:
            ReviewNotFoundError: If no review has that id.
            ReviewForbiddenError: If the review belongs to another user.
        """
        review = await self.repo.get_by_id(review_id)
        if review is None:
            raise ReviewNotFoundError(f"Review {review_id} not found")
        if review.user_id != user_id:
            raise ReviewForbiddenError("Not the author of this review")
        await self.repo.delete(review)
        await self.session.commit()

    # ------------------------------------------------------------------ #
    # Public list + aggregate
    # ------------------------------------------------------------------ #
    async def list_public(
        self,
        product_id: int,
        sort: ReviewSort,
        cursor: str | None,
    ) -> ProductReviewsOut:
        """Return approved reviews + the rating aggregate for a product (§4).

        Args:
            product_id: Product whose approved reviews to list.
            sort: ``newest`` (default) / ``highest`` / ``lowest``.
            cursor: Opaque cursor from a previous page (``None`` = first page).

        Returns:
            ProductReviewsOut: ``{aggregate, data, next_cursor}`` envelope.

        Raises:
            InvalidCursorError: If ``cursor`` is malformed or sort-mismatched.
        """
        after_id: int | None = None
        if cursor is not None:
            after_id = _decode_cursor(cursor, sort.value)

        rows = await self.repo.list_approved(
            product_id=product_id,
            sort=sort.value,
            after_id=after_id,
            limit=PUBLIC_PAGE_SIZE + 1,
        )
        has_more = len(rows) > PUBLIC_PAGE_SIZE
        page = rows[:PUBLIC_PAGE_SIZE]
        next_cursor: str | None = None
        if has_more and page:
            next_cursor = _encode_cursor(sort.value, page[-1].id)

        aggregate = await self._aggregate(product_id)
        return ProductReviewsOut(
            aggregate=aggregate,
            data=[PublicReviewOut.model_validate(r) for r in page],
            next_cursor=next_cursor,
        )

    async def _aggregate(self, product_id: int) -> RatingAggregate:
        """Build the approved-review aggregate for a product (§3).

        Args:
            product_id: Product to aggregate.

        Returns:
            RatingAggregate: avg (1 decimal, or ``None``), count and a full
            5..1 star distribution (missing stars filled with ``0``).
        """
        rows = await self.repo.rating_distribution(product_id)
        distribution = {star: 0 for star in range(MAX_RATING, 0, -1)}
        total = 0
        weighted = 0
        for rating, count in rows:
            distribution[rating] = count
            total += count
            weighted += rating * count
        average = round(weighted / total, 1) if total else None
        return RatingAggregate(
            average=average,
            count=total,
            distribution=distribution,
        )

    async def rating_summary(self, product_id: int) -> tuple[float | None, int]:
        """Return ``(rating_avg, rating_count)`` for a product's PDP detail (§3).

        Consumed by the catalog service to populate ``ProductOut.rating_avg`` /
        ``rating_count``. ``rating_avg`` is rounded to one decimal (``None`` with
        no approved reviews).

        Args:
            product_id: Product to summarize.

        Returns:
            tuple[float | None, int]: ``(average, count)``.
        """
        average, count = await self.repo.aggregate_for_product(product_id)
        return (round(average, 1) if average is not None else None, count)

    # ------------------------------------------------------------------ #
    # Admin moderation
    # ------------------------------------------------------------------ #
    async def list_admin(
        self,
        status: str | None,
        product_id: int | None,
        cursor: str | None,
    ) -> AdminReviewList:
        """Return a keyset-paginated moderation queue page (§4).

        Args:
            status: Optional exact status filter.
            product_id: Optional exact product filter.
            cursor: Opaque cursor (``None`` = first page); the queue is always
                ordered ``id`` desc, so the cursor sort key is fixed to ``id``.

        Returns:
            AdminReviewList: ``{data, next_cursor}`` envelope.

        Raises:
            InvalidCursorError: If ``cursor`` is malformed.
        """
        after_id: int | None = None
        if cursor is not None:
            after_id = _decode_cursor(cursor, "id")

        rows = await self.repo.list_for_admin(
            status=status,
            product_id=product_id,
            after_id=after_id,
            limit=ADMIN_PAGE_SIZE + 1,
        )
        has_more = len(rows) > ADMIN_PAGE_SIZE
        page = rows[:ADMIN_PAGE_SIZE]
        next_cursor: str | None = None
        if has_more and page:
            next_cursor = _encode_cursor("id", page[-1].id)
        return AdminReviewList(
            data=[AdminReviewOut.model_validate(r) for r in page],
            next_cursor=next_cursor,
        )

    async def moderate(self, review_id: int, status: str) -> Review:
        """Approve or reject a review, stamping ``moderated_at`` (§4).

        Args:
            review_id: The review to moderate.
            status: The target status (``approved`` / ``rejected``; validated by
                the request schema).

        Returns:
            Review: The updated review.

        Raises:
            ReviewNotFoundError: If no review has that id.
        """
        review = await self.repo.get_by_id(review_id)
        if review is None:
            raise ReviewNotFoundError(f"Review {review_id} not found")
        review.status = status
        review.moderated_at = datetime.now(UTC)
        await self.session.flush()
        await self.session.commit()
        # Refresh so the serialized response carries the DB-side timestamps
        # (created_at / updated_at) without a lazy-load in the async serializer.
        await self.session.refresh(review)
        return review

    async def delete_admin(self, review_id: int) -> None:
        """Hard-delete any review (staff, §4).

        Args:
            review_id: The review to delete.

        Raises:
            ReviewNotFoundError: If no review has that id.
        """
        review = await self.repo.get_by_id(review_id)
        if review is None:
            raise ReviewNotFoundError(f"Review {review_id} not found")
        await self.repo.delete(review)
        await self.session.commit()

    async def pending_count(self) -> int:
        """Return the number of reviews awaiting moderation (nav badge, §4).

        Returns:
            int: Count of ``pending`` reviews.
        """
        return await self.repo.pending_count()
