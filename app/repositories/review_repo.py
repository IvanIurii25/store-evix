"""Async data-access layer for product reviews (Reviews & Ratings, Phase 1).

Only SQL / query construction lives here — no business rules (those belong to
:class:`~app.services.review_service.ReviewService`) and no HTTP. Committing is
the caller's (service's) responsibility.

The verified-purchase check (:meth:`has_purchased`) is a single ``EXISTS`` over
``order`` joined to ``order_item`` (non-cancelled orders only, §2). The public
list and aggregate are two cheap queries backed by the ``(product_id, status)``
index; the moderation queue is keyset-paginated by ``id`` desc.
"""

from sqlalchemy import Row, and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import ORDER_STATUSES, Order, OrderItem
from app.models.review import STATUS_APPROVED, STATUS_PENDING, Review

# Order fulfilment status that excludes an order from counting as a purchase (§2).
_CANCELED_STATUS: str = "canceled"

# Sort → (column, descending) mapping for the public list (§4). ``id`` is the
# tiebreaker so pagination is deterministic under equal ratings/timestamps.
_NEWEST: str = "newest"
_HIGHEST: str = "highest"
_LOWEST: str = "lowest"

assert _CANCELED_STATUS in ORDER_STATUSES  # guards against an order-status rename


class ReviewRepository:
    """Read/write queries for reviews, bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used by every query method.

        Args:
            session: Active async session (request- or test-scoped).
        """
        self.session = session

    async def get(self, product_id: int, user_id: int) -> Review | None:
        """Return the review for ``(product_id, user_id)``, or ``None``.

        Args:
            product_id: Product the review is for.
            user_id: Authoring user.

        Returns:
            Review | None: The row, or ``None`` if absent.
        """
        stmt = select(Review).where(
            Review.product_id == product_id,
            Review.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, review_id: int) -> Review | None:
        """Return the review with the given id, or ``None``.

        Args:
            review_id: Primary key.

        Returns:
            Review | None: The row, or ``None`` if absent.
        """
        return await self.session.get(Review, review_id)

    async def has_purchased(self, product_id: int, user_id: int) -> bool:
        """Return whether the user has a non-cancelled order line for the product.

        Drives the ``is_verified`` snapshot (§2): ``EXISTS`` over ``order`` joined
        to ``order_item`` where the order belongs to the user, is not
        ``canceled``, and has a line for this product.

        Args:
            product_id: Product to check.
            user_id: The purchasing user.

        Returns:
            bool: ``True`` if such an order line exists.
        """
        stmt = select(
            select(OrderItem.id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.user_id == user_id,
                Order.status != _CANCELED_STATUS,
                OrderItem.product_id == product_id,
            )
            .exists()
        )
        return bool((await self.session.execute(stmt)).scalar())

    async def delete(self, review: Review) -> None:
        """Delete a review row. The caller commits.

        Args:
            review: The row to delete.
        """
        await self.session.delete(review)
        await self.session.flush()

    async def delete_all_for_user(self, user_id: int) -> None:
        """Delete every review authored by ``user_id`` (Art.17 erasure)."""
        await self.session.execute(delete(Review).where(Review.user_id == user_id))

    # ------------------------------------------------------------------ #
    # Public list + aggregate (approved only)
    # ------------------------------------------------------------------ #
    async def list_approved(
        self,
        product_id: int,
        sort: str,
        after_id: int | None,
        limit: int,
    ) -> list[Review]:
        """Return approved reviews for a product, keyset-paginated (§4).

        Args:
            product_id: Product to list reviews for.
            sort: ``newest`` / ``highest`` / ``lowest``.
            after_id: Exclusive lower bound (the last id of the previous page),
                or ``None`` for the first page.
            limit: Maximum rows to return (the caller fetches ``limit + 1`` to
                detect a further page).

        Returns:
            list[Review]: Approved reviews in the requested order.
        """
        stmt = select(Review).where(
            Review.product_id == product_id,
            Review.status == STATUS_APPROVED,
        )
        if after_id is not None:
            stmt = stmt.where(Review.id < after_id)
        if sort == _HIGHEST:
            stmt = stmt.order_by(Review.rating.desc(), Review.id.desc())
        elif sort == _LOWEST:
            stmt = stmt.order_by(Review.rating.asc(), Review.id.desc())
        else:  # newest (default)
            stmt = stmt.order_by(Review.id.desc())
        stmt = stmt.limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def rating_distribution(self, product_id: int) -> list[Row]:
        """Return ``(rating, count)`` rows for a product's approved reviews (§3).

        One grouped query backed by the ``(product_id, status)`` index; the
        service derives avg / total / per-star distribution from these rows.

        Args:
            product_id: Product to aggregate.

        Returns:
            list[Row]: ``(rating, count)`` rows (one per distinct rating present).
        """
        stmt = (
            select(Review.rating, func.count().label("count"))
            .where(
                Review.product_id == product_id,
                Review.status == STATUS_APPROVED,
            )
            .group_by(Review.rating)
        )
        return list((await self.session.execute(stmt)).all())

    async def aggregate_for_product(self, product_id: int) -> tuple[float | None, int]:
        """Return ``(average, count)`` of approved reviews for a product (§3).

        Powers ``ProductOut.rating_avg`` / ``rating_count`` on the PDP detail
        (single round-trip). ``average`` is ``None`` when there are no approved
        reviews (``count`` is then ``0``).

        Args:
            product_id: Product to aggregate.

        Returns:
            tuple[float | None, int]: ``(average, count)``.
        """
        stmt = select(
            func.avg(Review.rating),
            func.count(),
        ).where(
            Review.product_id == product_id,
            Review.status == STATUS_APPROVED,
        )
        avg, count = (await self.session.execute(stmt)).one()
        return (float(avg) if avg is not None else None, int(count))

    # ------------------------------------------------------------------ #
    # Admin moderation queue
    # ------------------------------------------------------------------ #
    async def list_for_admin(
        self,
        status: str | None,
        product_id: int | None,
        after_id: int | None,
        limit: int,
    ) -> list[Review]:
        """Return moderation-queue reviews, filtered + keyset-paginated (§4).

        Args:
            status: Optional exact status filter (``pending`` by default in the
                router; ``None`` here means no status filter).
            product_id: Optional exact product filter.
            after_id: Exclusive ``id`` upper bound (previous page's last id), or
                ``None`` for the first page.
            limit: Maximum rows to return (caller fetches ``limit + 1``).

        Returns:
            list[Review]: Reviews newest-first (``id`` desc).
        """
        conditions = []
        if status is not None:
            conditions.append(Review.status == status)
        if product_id is not None:
            conditions.append(Review.product_id == product_id)
        if after_id is not None:
            conditions.append(Review.id < after_id)
        stmt = select(Review)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(Review.id.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def pending_count(self) -> int:
        """Return the number of reviews awaiting moderation (nav badge, §4).

        Returns:
            int: Count of ``pending`` reviews.
        """
        stmt = (
            select(func.count())
            .select_from(Review)
            .where(Review.status == STATUS_PENDING)
        )
        return int((await self.session.execute(stmt)).scalar_one())
