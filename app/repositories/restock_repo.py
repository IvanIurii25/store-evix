"""Async data-access layer for restock subscriptions (Phase 1).

Only SQL / query construction lives here — no business rules (those belong to
:class:`~app.services.restock_service.RestockService`) and no HTTP. Committing is
the caller's (service's / task's) responsibility.

The notification query (:meth:`list_active_for_product`) joins each active
subscription to its :class:`AppUser` so the sending task has the recipient email
without an N+1; the account view (:meth:`list_active_for_user`) joins product +
translation for name/slug in one round-trip.
"""

from datetime import timedelta

from sqlalchemy import Row, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.catalog import (
    Category,
    CategoryTranslation,
    Media,
    Product,
    ProductTranslation,
)
from app.models.restock import (
    STATUS_ACTIVE,
    STATUS_NOTIFIED,
    RestockSubscription,
)
from app.models.user import AppUser

# Momentum window for the demand overview: subscriptions created within this
# many days count toward ``waiters_7d`` (§9).
_MOMENTUM_DAYS: int = 7

# Default language whose translation is coalesced onto rows missing the
# requested-language translation (mirrors the cart/order name fallback, §2.1.1).
_FALLBACK_LANG: str = "ro"


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

    async def demand_overview(self, lang: str) -> list[Row]:
        """Return one aggregate row per product with active restock waiters (§9).

        A single grouped query (no N+1): ``RestockSubscription`` filtered to
        active rows, grouped by ``product_id``. Counts total waiters and the
        7-day momentum (``FILTER (WHERE created_at > now() - interval '7 days')``).
        Joined to :class:`Product` for stock/price/publication, to
        :class:`ProductTranslation` for a localized name/slug (LEFT joins in the
        requested ``lang`` then the fallback lang, coalesced — mirrors the cart
        name fallback (§2.1.1) — with ``Product.code`` as the final name fallback
        so a row always renders), to :class:`Category` +
        :class:`CategoryTranslation` for the localized category name (LEFT, may be
        ``None``), and a correlated scalar subquery for the first media URL
        (ordered by ``position``, ``id``). Ordered by waiter count descending as a
        sensible server default; the front-end re-sorts.

        The result set is small (only products with at least one active waiter).

        Args:
            lang: Requested display language (validated / normalized by the caller).

        Returns:
            list[Row]: Rows with ``product_id, name, slug, category_name, qty,
                price, is_active, image_url, waiters, waiters_7d``.
        """
        req = aliased(ProductTranslation)
        dft = aliased(ProductTranslation)
        cat_req = aliased(CategoryTranslation)
        cat_dft = aliased(CategoryTranslation)
        cutoff = func.now() - timedelta(days=_MOMENTUM_DAYS)

        image_url = (
            select(Media.url)
            .where(Media.product_id == Product.id)
            .order_by(Media.position, Media.id)
            .limit(1)
            .correlate(Product)
            .scalar_subquery()
        )

        stmt = (
            select(
                RestockSubscription.product_id.label("product_id"),
                func.coalesce(req.name, dft.name, Product.code).label("name"),
                func.coalesce(req.slug, dft.slug).label("slug"),
                func.coalesce(cat_req.name, cat_dft.name).label("category_name"),
                Product.qty.label("qty"),
                Product.price.label("price"),
                Product.is_active.label("is_active"),
                image_url.label("image_url"),
                func.count().label("waiters"),
                func.count()
                .filter(RestockSubscription.created_at > cutoff)
                .label("waiters_7d"),
            )
            .join(Product, Product.id == RestockSubscription.product_id)
            .outerjoin(req, (req.product_id == Product.id) & (req.lang == lang))
            .outerjoin(
                dft, (dft.product_id == Product.id) & (dft.lang == _FALLBACK_LANG)
            )
            .outerjoin(Category, Category.id == Product.category_id)
            .outerjoin(
                cat_req,
                (cat_req.category_id == Category.id) & (cat_req.lang == lang),
            )
            .outerjoin(
                cat_dft,
                (cat_dft.category_id == Category.id) & (cat_dft.lang == _FALLBACK_LANG),
            )
            .where(RestockSubscription.status == STATUS_ACTIVE)
            .group_by(
                RestockSubscription.product_id,
                req.name,
                dft.name,
                req.slug,
                dft.slug,
                cat_req.name,
                cat_dft.name,
                Product.code,
                Product.qty,
                Product.price,
                Product.is_active,
                Product.id,
            )
            .order_by(func.count().desc())
        )
        return list((await self.session.execute(stmt)).all())
