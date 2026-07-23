"""Pydantic v2 schemas for the restock-notification feature (Phase 1, §5).

Customer contracts only (the admin waiter-count reuses a one-field response
defined next to its endpoint):

* :class:`RestockSubscribeIn` — the subscribe request body (product + lang).
* :class:`RestockSubscribedOut` — the button-state response (``{subscribed}``).
* :class:`RestockSubscriptionItem` — one "waiting for" entry in the account list.

``lang`` is validated at the edge against
:data:`~app.models.restock.ALLOWED_LANGS` so a bad value fails with 422, never a
DB CHECK.
"""

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
