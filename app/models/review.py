"""Product review + rating domain ORM model (Reviews & Ratings, Phase 1).

A logged-in customer leaves at most one review per product (the
``UNIQUE (product_id, user_id)`` constraint); editing an existing review sends it
back to ``pending`` for re-moderation (§6.1). Conventions mirror the restock /
content-page models (§2):

* ``lang`` and ``status`` are plain text constrained by ``CheckConstraint`` (no
  PostgreSQL ENUM types), matching the catalog language handling (§2.1.1).
* Both FKs are ``ON DELETE CASCADE`` so deleting a product or an account drops
  its reviews (GDPR-correct for the account case, §2).
* ``is_verified`` is a snapshot of "the user bought this product" taken at submit
  time (EXISTS a non-cancelled order line, §2). It is never recomputed on read.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# Supported storefront languages (ru + ro), applied as a CheckConstraint on the
# ``lang`` column instead of a PostgreSQL ENUM (mirrors catalog / restock §2.1.1).
ALLOWED_LANGS: tuple[str, ...] = ("ru", "ro")
_LANG_IN = ", ".join(f"'{lang}'" for lang in ALLOWED_LANGS)

# Moderation lifecycle (§D2): a new / edited review is ``pending`` until an
# operator ``approved`` or ``rejected`` it. Only ``approved`` reviews are public.
STATUS_PENDING: str = "pending"
STATUS_APPROVED: str = "approved"
STATUS_REJECTED: str = "rejected"
ALLOWED_STATUSES: tuple[str, ...] = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED)
_STATUS_IN = ", ".join(f"'{status}'" for status in ALLOWED_STATUSES)

# Rating bounds (§D3): 1..5 integer stars, mandatory (schema.org-friendly).
MIN_RATING: int = 1
MAX_RATING: int = 5


class Review(Base, TimestampMixin):
    """A customer's rating + optional text for one product (§2)."""

    __tablename__ = "review"
    __table_args__ = (
        UniqueConstraint("product_id", "user_id"),
        Index("ix_review_product_status", "product_id", "status"),
        Index("ix_review_status", "status"),
        CheckConstraint(
            f"rating BETWEEN {MIN_RATING} AND {MAX_RATING}",
            name="rating_range",
        ),
        CheckConstraint(f"status IN ({_STATUS_IN})", name="status_allowed"),
        CheckConstraint(f"lang IN ({_LANG_IN})", name="lang_allowed"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("product.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Display name snapshot — AppUser has no name; the form asks (optional),
    # else the storefront renders a fallback ("Покупатель" / "Client"). Never the
    # email.
    author_name: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default=STATUS_PENDING,
    )
    # Snapshot of "the user bought this product" at submit time (§2).
    is_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    # Storefront language at submit time.
    lang: Mapped[str] = mapped_column(String, nullable=False)
    # Set when an operator approves / rejects the review (§4 PATCH).
    moderated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
