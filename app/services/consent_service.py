"""Consent recording service (LP195/2024, Art.7 — provable consent).

Composes the immutable :class:`ConsentRecord` from a storefront decision. The
category snapshot is derived here (``necessary`` always true, ``analytics`` from
the opt-in flag) so the wire body stays a single boolean.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consent import CONSENT_POLICY_VERSION, ConsentRecord
from app.repositories.consent_repo import ConsentRepository

# Defensive cap mirroring the schema bound.
_ANON_ID_MAX: int = 64
_LANG_MAX: int = 8


class ConsentService:
    """Record consent decisions into the append-only log."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and the consent repository."""
        self.session = session
        self.repo = ConsentRepository(session)

    async def record(
        self,
        *,
        analytics: bool,
        action: str,
        source: str,
        lang: str,
        anonymous_id: str | None,
        user_id: int | None,
    ) -> ConsentRecord:
        """Persist one consent decision and return the stored row.

        Args:
            analytics: Whether the analytics category was granted.
            action: ``accept_all`` | ``reject_all`` | ``custom`` | ``withdraw``.
            source: ``banner`` | ``settings``.
            lang: Storefront language at decision time.
            anonymous_id: Guest client id (uuid), or ``None``.
            user_id: Authenticated user's id, or ``None`` for a guest.

        Returns:
            ConsentRecord: The persisted, immutable record.
        """
        record = ConsentRecord(
            anonymous_id=anonymous_id[:_ANON_ID_MAX] if anonymous_id else None,
            user_id=user_id,
            categories={"necessary": True, "analytics": analytics},
            lang=lang[:_LANG_MAX],
            policy_version=CONSENT_POLICY_VERSION,
            source=source,
            action=action,
        )
        await self.repo.add(record)
        await self.session.commit()
        return record
