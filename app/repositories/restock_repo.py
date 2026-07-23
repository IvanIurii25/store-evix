"""Async data-access layer for restock subscriptions (Phase 1).

Only SQL / query construction lives here — no business rules (those belong to
:class:`~app.services.restock_service.RestockService`) and no HTTP. Committing is
the caller's (service's / task's) responsibility.

The notification query (:meth:`list_active_for_product`) joins each active
subscription to its :class:`AppUser` so the sending task has the recipient email
without an N+1; the account view (:meth:`list_active_for_user`) joins product +
translation for name/slug in one round-trip.
"""

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Product, ProductTranslation
from app.models.restock import (
    STATUS_ACTIVE,
    STATUS_NOTIFIED,
    RestockSubscription,
)
from app.models.user import AppUser


class RestockRepository:
    """Read/write queries for restock subscriptions, bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used by every query method.

        Args:
            session: Active async session (request-, task- or test-scoped).
        """
        self.session = session

    async def get(
        self,
        product_id: int,
        user_id: int,
    ) -> RestockSubscription | None:
        """Return the subscription for ``(product_id, user_id)``, or ``None``.

        Args:
            product_id: Product the subscription is for.
            user_id: Owning user.

        Returns:
            RestockSubscription | None: The row, or ``None`` if absent.
        """
        stmt = select(RestockSubscription).where(
            RestockSubscription.product_id == product_id,
            RestockSubscription.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert_active(
        self,
        product_id: int,
        user_id: int,
        lang: str,
    ) -> RestockSubscription:
        """Create an active subscription or reactivate a spent one (idempotent).

        * No row → create one (``active``).
        * Existing ``notified`` row → reactivate: ``active`` + clear
          ``notified_at`` and refresh ``lang``.
        * Existing ``active`` row → return unchanged (refreshing ``lang`` to the
          latest storefront language).

        The caller commits.

        Args:
            product_id: Product to subscribe to.
            user_id: Subscribing user.
            lang: Storefront language at subscribe time (drives the email).

        Returns:
            RestockSubscription: The active subscription row (flushed).
        """
        subscription = await self.get(product_id, user_id)
        if subscription is None:
            subscription = RestockSubscription(
                product_id=product_id,
                user_id=user_id,
                lang=lang,
                status=STATUS_ACTIVE,
            )
            self.session.add(subscription)
        else:
            subscription.lang = lang
            subscription.status = STATUS_ACTIVE
            subscription.notified_at = None
        await self.session.flush()
        return subscription

    async def delete(self, product_id: int, user_id: int) -> None:
        """Delete the subscription for ``(product_id, user_id)`` if present.

        The caller commits.

        Args:
            product_id: Product the subscription is for.
            user_id: Owning user.
        """
        subscription = await self.get(product_id, user_id)
        if subscription is not None:
            await self.session.delete(subscription)
            await self.session.flush()

    async def list_active_for_product(
        self,
        product_id: int,
    ) -> list[tuple[RestockSubscription, str]]:
        """Return active subscriptions for a product paired with recipient email.

        Joined to :class:`AppUser` so the sending task has ``email`` without an
        N+1. Only active (unspent) subscriptions are returned.

        Args:
            product_id: Product that came back in stock.

        Returns:
            list[tuple[RestockSubscription, str]]: ``(subscription, email)`` rows.
        """
        stmt = (
            select(RestockSubscription, AppUser.email)
            .join(AppUser, AppUser.id == RestockSubscription.user_id)
            .where(
                RestockSubscription.product_id == product_id,
                RestockSubscription.status == STATUS_ACTIVE,
            )
        )
        rows = (await self.session.execute(stmt)).all()
        return [(subscription, email) for subscription, email in rows]

    async def mark_notified(self, ids: list[int]) -> None:
        """Mark the given subscriptions as notified (``status`` + ``notified_at``).

        The caller commits.

        Args:
            ids: Subscription ids that were successfully emailed.
        """
        if not ids:
            return
        stmt = (
            update(RestockSubscription)
            .where(RestockSubscription.id.in_(ids))
            .values(status=STATUS_NOTIFIED, notified_at=func.now())
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def count_active_for_product(self, product_id: int) -> int:
        """Return the number of active subscribers for a product (demand signal).

        Args:
            product_id: Product to count waiters for.

        Returns:
            int: The count of active subscriptions.
        """
        stmt = (
            select(func.count())
            .select_from(RestockSubscription)
            .where(
                RestockSubscription.product_id == product_id,
                RestockSubscription.status == STATUS_ACTIVE,
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def list_active_for_user(
        self,
        user_id: int,
        lang: str,
    ) -> list[tuple[RestockSubscription, ProductTranslation]]:
        """Return a user's active subscriptions joined to product translations.

        Only subscriptions whose product has a translation for ``lang`` are
        returned (name/slug come from that translation). One round-trip, no N+1.

        Args:
            user_id: Owning user.
            lang: Language to resolve product name/slug in.

        Returns:
            list[tuple[RestockSubscription, ProductTranslation]]: Ordered pairs
            (newest subscription first).
        """
        stmt = (
            select(RestockSubscription, ProductTranslation)
            .join(
                ProductTranslation,
                (ProductTranslation.product_id == RestockSubscription.product_id)
                & (ProductTranslation.lang == lang),
            )
            .where(
                RestockSubscription.user_id == user_id,
                RestockSubscription.status == STATUS_ACTIVE,
            )
            .order_by(RestockSubscription.id.desc())
        )
        rows = (await self.session.execute(stmt)).all()
        return [(subscription, translation) for subscription, translation in rows]

    async def products_with_active_and_in_stock(self) -> list[int]:
        """Return product ids that have active subscriptions AND ``qty > 0``.

        Drives the reconciliation sweep (§3) — catches any stock growth,
        including paths that bypass :meth:`update_product`.

        Returns:
            list[int]: Distinct product ids to (re)notify.
        """
        stmt = (
            select(RestockSubscription.product_id)
            .join(Product, Product.id == RestockSubscription.product_id)
            .where(
                RestockSubscription.status == STATUS_ACTIVE,
                Product.qty > 0,
            )
            .distinct()
        )
        return list((await self.session.execute(stmt)).scalars().all())
