"""Pydantic v2 schemas for the public consent endpoint (LP195/2024, Art.7).

The storefront cookie banner posts one :class:`ConsentIn` per decision (accept /
reject / change). ``necessary`` is implicit and always granted; only
``analytics`` is opt-in, so the body carries a single boolean plus the metadata
needed to make the record provable (action, source, language).
"""

from typing import Literal

from pydantic import BaseModel, Field


class ConsentIn(BaseModel):
    """Body of ``POST /consent`` — one consent decision from the storefront."""

    analytics: bool
    action: Literal["accept_all", "reject_all", "custom", "withdraw"]
    source: Literal["banner", "settings"] = "banner"
    lang: str = Field(min_length=2, max_length=8)
    # Rotating client id (uuid) for guests; the backend never generates it.
    anonymous_id: str | None = Field(default=None, max_length=64)


class ConsentAck(BaseModel):
    """Acknowledgement echoing the policy version the client should store."""

    ok: bool = True
    policy_version: int
