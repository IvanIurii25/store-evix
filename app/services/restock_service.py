"""Restock-notification business logic (Phase 1, §5 / §7).

Sits between the router / Celery task and :class:`RestockRepository`. Owns the
domain rules:

* You may only subscribe to a product that exists AND is out of stock
  (``qty <= 0``) — the button is only shown on OOS cards, and the backend
  enforces it (:class:`ProductInStockError` → 400).
* Subscribing is idempotent and reactivates a spent (``notified``) row.
* :meth:`notify_product` (used by the Celery task) loads active subscribers,
  emails each with a bounded-concurrency ``asyncio.gather`` and per-item
  ``try/except`` (one failed send never blocks the rest), then marks only the
  ones that were actually sent as ``notified``.

The raised :class:`RestockError` subclasses carry their own ``status_code`` and
``code``; the unified :class:`~app.core.errors.DomainError` handler renders them.
"""

import asyncio
import logging

from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.lang import normalize_lang
from app.core.config import settings
from app.core.email import send_restock_notification
from app.core.errors import DomainError
from app.models.catalog import ProductTranslation
from app.models.restock import STATUS_ACTIVE, RestockSubscription
from app.repositories.catalog_repo import CatalogRepository
from app.repositories.restock_repo import RestockRepository
from app.schemas.restock import DemandItem, RestockSubscriptionItem

logger = logging.getLogger(__name__)

# How many restock emails to send concurrently per gather batch (§4). Bounded so
# a large waiter list never opens hundreds of SMTP connections at once.
_SEND_CHUNK_SIZE: int = 10


class RestockError(DomainError):
    """Base class for restock domain errors (rendered by the unified handler)."""


class RestockNotFoundError(RestockError):
    """The referenced product does not exist."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ProductInStockError(RestockError):
    """Rejected a subscription because the product is currently in stock (§8.1)."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "in_stock"


class RestockUnsupportedError(RestockError):
    """Restock subscriptions are not supported for this product (v1: variable)."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "unsupported"


class RestockService:
    """Restock-subscription operations bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and its repository.

        Args:
            session: Active async session used for all reads and writes.
        """
        self.session = session
        self.repo = RestockRepository(session)
        self.catalog = CatalogRepository(session)

    async def subscribe(
        self,
        product_id: int,
        user_id: int,
        lang: str,
    ) -> RestockSubscription:
        """Subscribe a user to a product's restock notification (idempotent, §5).

        Args:
            product_id: Product to subscribe to.
            user_id: Subscribing user.
            lang: Storefront language at subscribe time (drives the email).

        Returns:
            RestockSubscription: The active subscription.

        Raises:
            RestockNotFoundError: If the product does not exist.
            RestockUnsupportedError: If the product is variable (v1 opt-out).
            ProductInStockError: If the product is currently in stock (``qty>0``).
        """
        product = await self.catalog.get_product(product_id)
        if product is None:
            raise RestockNotFoundError(f"Product {product_id} not found")
        # v1: variable products opt out of restock — stock is per-variant, so a
        # product-level subscription is meaningless (per-variant restock is P6).
        if product.has_variants:
            raise RestockUnsupportedError(
                f"Product {product_id} is variable; restock is not available"
            )
        if product.qty > 0:
            raise ProductInStockError(
                f"Product {product_id} is in stock; subscription not needed"
            )
        subscription = await self.repo.upsert_active(product_id, user_id, lang)
        await self.session.commit()
        return subscription

    async def unsubscribe(self, product_id: int, user_id: int) -> None:
        """Remove a user's subscription for a product (no-op if absent, §5).

        Args:
            product_id: Product to unsubscribe from.
            user_id: Owning user.
        """
        await self.repo.delete(product_id, user_id)
        await self.session.commit()

    async def is_subscribed(self, product_id: int, user_id: int) -> bool:
        """Return whether the user has an active subscription for the product.

        Args:
            product_id: Product to check.
            user_id: Owning user.

        Returns:
            bool: ``True`` if an active subscription exists.
        """
        subscription = await self.repo.get(product_id, user_id)
        return subscription is not None and subscription.status == STATUS_ACTIVE

    async def list_my(
        self,
        user_id: int,
        lang: str,
    ) -> list[RestockSubscriptionItem]:
        """Return a user's active "waiting for" items resolved in ``lang`` (§5).

        Args:
            user_id: Owning user.
            lang: Language to resolve product name/slug in.

        Returns:
            list[RestockSubscriptionItem]: ``(product_id, slug, name)`` entries.
        """
        rows = await self.repo.list_active_for_user(user_id, lang)
        return [
            RestockSubscriptionItem(
                product_id=subscription.product_id,
                slug=translation.slug,
                name=translation.name,
            )
            for subscription, translation in rows
        ]

    async def waiter_count(self, product_id: int) -> int:
        """Return the number of active waiters for a product (admin signal, §9).

        Args:
            product_id: Product to count waiters for.

        Returns:
            int: The count of active subscriptions.
        """
        return await self.repo.count_active_for_product(product_id)

    async def demand_overview(self, lang: str) -> list[DemandItem]:
        """Return the admin restock-demand overview resolved in ``lang`` (§9).

        Normalizes the language (falling back to the default for anything
        unsupported), runs the single aggregate query and maps each row to a
        :class:`DemandItem` (deriving ``in_stock`` and stringifying the price).

        Args:
            lang: Requested display language (query value, may be unsupported).

        Returns:
            list[DemandItem]: One item per product with active waiters, ordered
            by waiter count descending.
        """
        rows = await self.repo.demand_overview(normalize_lang(lang))
        return [
            DemandItem.from_row(
                product_id=row.product_id,
                name=row.name,
                slug=row.slug,
                category=row.category_name,
                qty=row.qty,
                price=row.price,
                is_active=row.is_active,
                image_url=row.image_url,
                waiters=row.waiters,
                waiters_7d=row.waiters_7d,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # Notification (Celery task path, §4 / §7)
    # ------------------------------------------------------------------ #
    async def notify_product(self, session: AsyncSession, product_id: int) -> int:
        """Email every active subscriber of a restocked product, then mark sent.

        Loads active subscriptions (+ emails), resolves each subscriber's
        product URL from the ``product_translation`` for their subscription
        language (skipping any with no translation in that language), sends the
        emails in bounded-concurrency batches with per-item error isolation, and
        marks only the successfully-sent subscriptions as ``notified``.

        Args:
            session: The active session (same one the service is bound to; passed
                explicitly so the Celery bridge can hand its task session in).
            product_id: The product that returned to stock.

        Returns:
            int: The number of subscribers successfully notified.
        """
        rows = await self.repo.list_active_for_product(product_id)
        if not rows:
            return 0

        # Resolve slugs for the languages present, in one query, to avoid N+1.
        langs = {subscription.lang for subscription, _email in rows}
        slugs = await self._load_slugs(session, product_id, langs)

        # Build the send jobs, skipping subscribers with no slug in their lang.
        jobs: list[tuple[int, str, str, str, str]] = []
        for subscription, email in rows:
            slug_name = slugs.get(subscription.lang)
            if slug_name is None:
                logger.warning(
                    "restock: no %s translation for product %s; skipping sub %s",
                    subscription.lang,
                    product_id,
                    subscription.id,
                )
                continue
            slug, name = slug_name
            url = f"{settings.storefront_base_url}/{subscription.lang}/p/{slug}"
            jobs.append((subscription.id, email, name, url, subscription.lang))

        sent_ids = await self._send_all(jobs)
        if sent_ids:
            await self.repo.mark_notified(sent_ids)
            await session.commit()
        return len(sent_ids)

    @staticmethod
    async def _load_slugs(
        session: AsyncSession,
        product_id: int,
        langs: set[str],
    ) -> dict[str, tuple[str, str]]:
        """Return ``{lang: (slug, name)}`` for a product across the given langs.

        Args:
            session: Active session.
            product_id: The product whose translations to load.
            langs: The set of languages needed.

        Returns:
            dict[str, tuple[str, str]]: One entry per language that has a
            translation.
        """
        if not langs:
            return {}
        stmt = select(
            ProductTranslation.lang,
            ProductTranslation.slug,
            ProductTranslation.name,
        ).where(
            ProductTranslation.product_id == product_id,
            ProductTranslation.lang.in_(langs),
        )
        rows = (await session.execute(stmt)).all()
        return {lang: (slug, name) for lang, slug, name in rows}

    @staticmethod
    async def _send_all(
        jobs: list[tuple[int, str, str, str, str]],
    ) -> list[int]:
        """Send all restock emails in bounded batches; return the sent sub ids.

        Each send is isolated with ``try/except`` so a single SMTP failure never
        aborts the batch — only successful sends are reported back for marking.

        Args:
            jobs: ``(subscription_id, email, name, url, lang)`` tuples.

        Returns:
            list[int]: Ids of subscriptions whose email was sent successfully.
        """
        sent_ids: list[int] = []
        for start in range(0, len(jobs), _SEND_CHUNK_SIZE):
            chunk = jobs[start : start + _SEND_CHUNK_SIZE]
            results = await asyncio.gather(
                *(
                    RestockService._send_one(sub_id, email, name, url, lang)
                    for sub_id, email, name, url, lang in chunk
                )
            )
            sent_ids.extend(sub_id for sub_id in results if sub_id is not None)
        return sent_ids

    @staticmethod
    async def _send_one(
        sub_id: int,
        email: str,
        name: str,
        url: str,
        lang: str,
    ) -> int | None:
        """Send one restock email; return the sub id on success, ``None`` on error.

        Args:
            sub_id: The subscription id (for marking).
            email: Recipient account email.
            name: Product display name in the subscription language.
            url: Absolute storefront product URL.
            lang: Subscription language.

        Returns:
            int | None: ``sub_id`` if the email was sent, else ``None``.
        """
        try:
            await send_restock_notification(
                to=email,
                product_name=name,
                product_url=url,
                lang=lang,
            )
        except Exception:  # noqa: BLE001 — one failure must not block the batch
            logger.exception(
                "restock: failed to send notification for subscription %s", sub_id
            )
            return None
        return sub_id
