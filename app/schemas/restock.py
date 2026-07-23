"""Pydantic v2 schemas for the restock-notification feature (Phase 1, §5).

Customer contracts plus the admin demand aggregate:

* :class:`RestockSubscribeIn` — the subscribe request body (product + lang).
* :class:`RestockSubscribedOut` — the button-state response (``{subscribed}``).
* :class:`RestockSubscriptionItem` — one "waiting for" entry in the account list.
* :class:`RestockWaitersOut` — the single-product admin waiter count (§9).
* :class:`DemandItem` — one row of the admin restock-demand overview (§9).

``lang`` is validated at the edge against
:data:`~app.models.restock.ALLOWED_LANGS` so a bad value fails with 422, never a
DB CHECK.
"""

from decimal import Decimal

from pydantic import BaseModel, field_validator

from app.models.restock import ALLOWED_LANGS


def _validate_lang(value: str) -> str:
    """Reject any language code outside the supported set.

    Args:
        value: Candidate language code.

    Returns:
        str: The validated language code.

    Raises:
        ValueError: If the code is not one of :data:`ALLOWED_LANGS`.
    """
    if value not in ALLOWED_LANGS:
        raise ValueError(f"lang must be one of {sorted(ALLOWED_LANGS)}")
    return value


class RestockSubscribeIn(BaseModel):
    """Request body to subscribe to a product's restock notification."""

    product_id: int
    lang: str

    @field_validator("lang")
    @classmethod
    def _check_lang(cls, value: str) -> str:
        return _validate_lang(value)


class RestockSubscribedOut(BaseModel):
    """Button-state response: whether the caller is subscribed to the product."""

    subscribed: bool


class RestockSubscriptionItem(BaseModel):
    """One active "waiting for" entry in the account list (§5, GET list)."""

    product_id: int
    slug: str
    name: str


class RestockWaitersOut(BaseModel):
    """Admin demand signal: number of active waiters for a product (§9)."""

    count: int


class DemandItem(BaseModel):
    """One product in the admin restock-demand overview (§9).

    Aggregates the active-waiter demand for a single out-of-stock (or any)
    product so the operator can decide what to restock. ``price`` is serialized
    as a string (like other money fields) so front-end arithmetic (waiters ×
    price) is exact. ``in_stock`` is derived server-side from ``qty``.
    """

    product_id: int
    name: str
    slug: str | None
    category: str | None
    qty: int
    in_stock: bool
    price: str
    is_active: bool
    image_url: str | None
    waiters: int
    waiters_7d: int

    @classmethod
    def from_row(
        cls,
        product_id: int,
        name: str,
        slug: str | None,
        category: str | None,
        qty: int,
        price: Decimal,
        is_active: bool,
        image_url: str | None,
        waiters: int,
        waiters_7d: int,
    ) -> "DemandItem":
        """Build a :class:`DemandItem` from a raw aggregate row.

        Derives ``in_stock`` from ``qty`` and renders ``price`` as a string.

        Args:
            product_id: Product primary key.
            name: Localized product name (or a fallback code).
            slug: Localized product slug, or ``None`` if untranslated.
            category: Localized category name, or ``None``.
            qty: Current stock level.
            price: Unit price.
            is_active: Whether the product is published.
            image_url: First media URL, or ``None`` if the product has no media.
            waiters: Count of active restock subscriptions.
            waiters_7d: Active subscriptions created in the last 7 days.

        Returns:
            DemandItem: The mapped response item.
        """
        return cls(
            product_id=product_id,
            name=name,
            slug=slug,
            category=category,
            qty=qty,
            in_stock=qty > 0,
            price=str(price),
            is_active=is_active,
            image_url=image_url,
            waiters=waiters,
            waiters_7d=waiters_7d,
        )
