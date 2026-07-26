"""Pydantic v2 schemas for the card-payment (maib) domain.

:class:`MaibCallbackPayload` models the webhook maib POSTs to our callback URL;
its ``result`` block is kept as a permissive dict because the signature is
computed over the *exact* keys maib sends (sorting them recursively), so the
verifier must see them untouched rather than a narrowed projection.
"""

from pydantic import BaseModel, ConfigDict, Field


class MaibCallbackPayload(BaseModel):
    """Inbound maib callback body: ``{result: {...}, signature: "..."}``.

    ``result`` is accepted as a raw dict on purpose — the signature check hashes
    the provider's own key/value set (recursively sorted), so narrowing the shape
    here would break verification for any field maib adds.
    """

    model_config = ConfigDict(extra="ignore")

    result: dict = Field(..., description="maib payment result block (signed).")
    signature: str = Field(..., description="Base64 SHA-256 signature of result.")


class SiteConfigOut(BaseModel):
    """Public storefront runtime config (``GET /site/config``).

    Currently exposes only the card-payment availability flag so the checkout UI
    can decide whether to render the card option next to COD.
    """

    card_payment_enabled: bool = Field(
        ...,
        description="Whether the card (maib) payment option is available.",
    )
