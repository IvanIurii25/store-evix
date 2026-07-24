"""Persistence for consent records (append-only, Art.7)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consent import ConsentRecord


class ConsentRepository:
    """Append-only writes to the consent log."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to an async session."""
        self.session = session

    async def add(self, record: ConsentRecord) -> ConsentRecord:
        """Insert one consent record (never update) and return it flushed."""
        self.session.add(record)
        await self.session.flush()
        return record
