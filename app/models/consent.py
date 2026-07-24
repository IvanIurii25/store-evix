"""Cookie/privacy consent domain model (LP195/2024 = GDPR, Art.7 accountability).

A single append-only :class:`ConsentRecord` per consent decision — the store must
be able to *demonstrate* that consent was given (or refused/withdrawn), so rows
are never updated: each new decision is a fresh row.

Deliberately minimal and privacy-first (consistent with the first-party analytics
design): we store the chosen categories, the storefront language, the policy
version, the source (banner/settings) and the action — **no IP and no
User-Agent** (proof of *when* + *what* + an optional rotating ``anonymous_id`` is
sufficient; storing IP would add exactly the kind of data the design avoids).
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Bump when the cookie/privacy policy changes materially → clients whose stored
# consent carries an older version are re-prompted by the banner.
CONSENT_POLICY_VERSION: int = 1

# Consent categories in scope for v1. ``necessary`` is always granted; only
# ``analytics`` (the first-party ``sid``/pageview cookie) is opt-in. No
# marketing/functional trackers exist, so they are intentionally absent.
CONSENT_SOURCES: tuple[str, ...] = ("banner", "settings")
CONSENT_ACTIONS: tuple[str, ...] = (
    "accept_all",
    "reject_all",
    "custom",
    "withdraw",
)
_SOURCE_IN = ", ".join(f"'{s}'" for s in CONSENT_SOURCES)
_ACTION_IN = ", ".join(f"'{a}'" for a in CONSENT_ACTIONS)


class ConsentRecord(Base):
    """One immutable consent decision (append-only proof of consent, Art.7)."""

    __tablename__ = "consent_record"
    __table_args__ = (
        CheckConstraint(f"source IN ({_SOURCE_IN})", name="consent_source_valid"),
        CheckConstraint(f"action IN ({_ACTION_IN})", name="consent_action_valid"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # Rotating client id for guests (a uuid stored in a necessary cookie); links
    # a guest's consent decisions without identifying them. Null when unknown.
    anonymous_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Attached when the visitor is logged in. ON DELETE SET NULL so the consent
    # record survives account erasure (the proof must outlive the account).
    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("app_user.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Snapshot of the chosen categories, e.g. ``{"necessary": true, "analytics": false}``.
    categories: Mapped[dict] = mapped_column(JSONB, nullable=False)
    lang: Mapped[str] = mapped_column(String, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
