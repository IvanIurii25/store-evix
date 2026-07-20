"""Data-access layer for staff-user management in the back office (admin §6.4).

Pure SQL/ORM access — no business rules, no HTTP. Staff are just
:class:`AppUser` rows with ``is_staff = True``; this repo reads the staff roster
and looks users up by id / email so the service can create-or-promote. Committing
is the caller's (service's) responsibility.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import AppUser


class AdminStaffRepository:
    """Async data access for the staff roster, bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used for all queries.

        Args:
            session: The active async DB session.
        """
        self.session = session

    async def list_staff(self) -> list[AppUser]:
        """Return all staff users (``is_staff``), newest first."""
        stmt = (
            select(AppUser)
            .where(AppUser.is_staff.is_(True))
            .order_by(AppUser.id.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(self, user_id: int) -> AppUser | None:
        """Fetch a user by primary key, or ``None`` if absent."""
        return await self.session.get(AppUser, user_id)

    async def get_by_email(self, email: str) -> AppUser | None:
        """Fetch a user by (case-insensitive) email, or ``None`` if absent."""
        stmt = select(AppUser).where(AppUser.email == email)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, user: AppUser) -> AppUser:
        """Persist a new user row and return it with a generated id."""
        self.session.add(user)
        await self.session.flush()
        await self.session.refresh(user)
        return user
