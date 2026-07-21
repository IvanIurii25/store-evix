"""Analytics domain model: :class:`Visit` — first-party pageview log (§6.3).

Privacy-first, first-party traffic tracking: one row per storefront page view,
carrying only what the back office needs to reason about traffic and load — the
path, an optional referrer, a rotating first-party ``session_id`` (assigned by
the storefront, not linked to identity), a coarse ``device`` class, a bot flag,
and an optional ``user_id`` when the visitor happens to be logged in.

No IP address and no raw User-Agent are stored — the device class is derived at
ingest and the UA discarded — so the table holds aggregate-friendly, minimized
data (see the admin-panel spec §5 on legality). ``user_id`` uses no FK so a
deleted account never blocks trimming/retention of the visit log.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Coarse device classes derived from the User-Agent at ingest (§6.3).
DEVICE_CLASSES: tuple[str, ...] = ("desktop", "mobile", "tablet", "bot")


class Visit(Base):
    """One storefront pageview (§6.3)."""

    __tablename__ = "visit"
    __table_args__ = (
        # The dashboard queries filter/group by time; index it for range scans.
        Index("ix_visit_occurred_at", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Naive UTC, matching the app-wide TimestampMixin convention so the shared
    # dashboard date-window (also naive) compares cleanly (§6.3).
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(),
        nullable=False,
        server_default=func.now(),
    )
    path: Mapped[str] = mapped_column(String, nullable=False)
    referrer: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    device: Mapped[str] = mapped_column(String, nullable=False, default="desktop")
    is_bot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
