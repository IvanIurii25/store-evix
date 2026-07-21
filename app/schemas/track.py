"""Pydantic v2 schemas for the public pageview-tracking endpoint (§6.3).

The storefront posts one :class:`PageviewIn` per navigation. Fields are bounded
defensively (the service also truncates); ``session_id`` is the storefront's
first-party cookie, not an identity token.
"""

from pydantic import BaseModel, Field


class PageviewIn(BaseModel):
    """Body of ``POST /track/pageview`` — one storefront navigation (§6.3)."""

    path: str = Field(min_length=1, max_length=2048)
    session_id: str = Field(min_length=1, max_length=128)
    referrer: str | None = Field(default=None, max_length=2048)


class TrackAck(BaseModel):
    """Minimal acknowledgement (the client ignores the body)."""

    ok: bool = True
