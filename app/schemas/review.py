"""Pydantic v2 schemas for the Reviews & Ratings feature (Phase 1, §4).

Customer contracts (submit / mine / public list + aggregate) and the admin
moderation contracts:

* :class:`ReviewSubmitIn` — the create-or-update request body (§4 POST).
* :class:`ReviewOut` — the caller's own review of any status (§4 GET mine).
* :class:`PublicReviewOut` — one approved review in the public list.
* :class:`RatingAggregate` — avg / count / per-star distribution (§3).
* :class:`ProductReviewsOut` — the public list envelope ``{aggregate, data,
  next_cursor}``.
* :class:`AdminReviewOut` — one moderation-queue row (§4 admin).
* :class:`AdminReviewList` — the moderation-queue envelope.
* :class:`ReviewModerateIn` — the ``{status}`` body for approve / reject.
* :class:`PendingCountOut` — the navigation badge count.

``rating`` is bounded 1..5 and ``lang`` is validated against
:data:`~app.models.review.ALLOWED_LANGS` at the edge so a bad value fails with
422 rather than a DB CHECK.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.review import (
    ALLOWED_LANGS,
    MAX_RATING,
    MIN_RATING,
    STATUS_APPROVED,
    STATUS_REJECTED,
)


class ReviewSort(str, Enum):
    """Allowed sort orders for the public review list (§4)."""

    NEWEST = "newest"
    HIGHEST = "highest"
    LOWEST = "lowest"


class ReviewSubmitIn(BaseModel):
    """Request body to create or update the caller's review of a product (§4)."""

    product_id: int
    rating: int = Field(ge=MIN_RATING, le=MAX_RATING)
    title: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=4000)
    author_name: str | None = Field(default=None, max_length=100)
    lang: str = "ro"

    @field_validator("lang")
    @classmethod
    def _check_lang(cls, value: str) -> str:
        """Reject any language code outside the supported set."""
        if value not in ALLOWED_LANGS:
            raise ValueError(f"lang must be one of {sorted(ALLOWED_LANGS)}")
        return value

    @field_validator("title", "body", "author_name")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        """Collapse an empty / whitespace-only optional string to ``None``."""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class ReviewOut(BaseModel):
    """The caller's own review of any status, for the edit form (§4 GET mine)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    rating: int
    title: str | None
    body: str | None
    author_name: str | None
    status: str
    is_verified: bool
    lang: str
    created_at: datetime
    updated_at: datetime


class PublicReviewOut(BaseModel):
    """One approved review in the public product list (§4 public list)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    rating: int
    title: str | None
    body: str | None
    author_name: str | None
    is_verified: bool
    created_at: datetime


class RatingAggregate(BaseModel):
    """Approved-review aggregate for a product: avg, count, star distribution (§3).

    ``distribution`` maps each star value ("5".."1") to how many approved reviews
    gave it, so the storefront can render the per-star bars without a second
    request. ``average`` is rounded to one decimal (``None`` when there are no
    approved reviews).
    """

    average: float | None
    count: int
    distribution: dict[int, int]


class ProductReviewsOut(BaseModel):
    """Public product-reviews envelope: aggregate + a cursor page of reviews (§4)."""

    aggregate: RatingAggregate
    data: list[PublicReviewOut]
    next_cursor: str | None = None


class AdminReviewOut(BaseModel):
    """One row of the admin moderation queue (§4 admin list)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    user_id: int
    rating: int
    title: str | None
    body: str | None
    author_name: str | None
    status: str
    is_verified: bool
    lang: str
    created_at: datetime
    moderated_at: datetime | None


class AdminReviewList(BaseModel):
    """Cursor-paginated moderation-queue envelope ``{data, next_cursor}`` (§4)."""

    data: list[AdminReviewOut]
    next_cursor: str | None = None


class ReviewModerateIn(BaseModel):
    """Moderation decision body: approve or reject a review (§4 PATCH)."""

    status: str

    @field_validator("status")
    @classmethod
    def _check_status(cls, value: str) -> str:
        """Only ``approved`` / ``rejected`` are valid moderation targets."""
        allowed = {STATUS_APPROVED, STATUS_REJECTED}
        if value not in allowed:
            raise ValueError(f"status must be one of {sorted(allowed)}")
        return value


class PendingCountOut(BaseModel):
    """Navigation badge: number of reviews awaiting moderation (§4)."""

    count: int
