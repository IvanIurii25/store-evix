"""Data-access layer for :class:`AppUser` and :class:`Address` (B4).

Pure SQL/ORM access — no business rules, no HTTP. All methods are async and
operate on an injected :class:`AsyncSession`; committing is the caller's
(service's) responsibility so a service can group writes in one transaction.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Address, AppUser


class UserRepository:
    """Async data access for users and their addresses.

    The session is injected once (repeated across every method, §15).
    """

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used for all queries.

        Args:
            session: The active async DB session.
        """
        self.session = session

    async def get_by_id(self, user_id: int) -> AppUser | None:
        """Fetch a user by primary key, or ``None`` if absent."""
        return await self.session.get(AppUser, user_id)

    async def get_by_email(self, email: str) -> AppUser | None:
        """Fetch a user by (case-insensitive) email, or ``None`` if absent."""
        stmt = select(AppUser).where(AppUser.email == email)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_phone(self, phone: str) -> AppUser | None:
        """Fetch a user by phone, or ``None`` if absent."""
        stmt = select(AppUser).where(AppUser.phone == phone)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create_user(
        self,
        email: str,
        password_hash: str,
        phone: str | None = None,
    ) -> AppUser:
        """Persist a new user and return it with a generated id.

        Args:
            email: Login email (unique, case-insensitive).
            password_hash: Pre-computed argon2 hash (never the plaintext).
            phone: Optional unique phone.

        Returns:
            AppUser: The freshly created user.
        """
        user = AppUser(email=email, password_hash=password_hash, phone=phone)
        self.session.add(user)
        await self.session.flush()
        await self.session.refresh(user)
        return user

    async def list_addresses(self, user_id: int) -> list[Address]:
        """Return all addresses owned by ``user_id`` ordered by id."""
        stmt = select(Address).where(Address.user_id == user_id).order_by(Address.id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_address(self, user_id: int, address_id: int) -> Address | None:
        """Fetch one address scoped to its owner, or ``None`` if absent."""
        stmt = select(Address).where(
            Address.id == address_id,
            Address.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def clear_default(self, user_id: int) -> None:
        """Unset ``is_default`` on every address of ``user_id``.

        Used before setting a new default so exactly one default remains.
        """
        for address in await self.list_addresses(user_id):
            if address.is_default:
                address.is_default = False
        await self.session.flush()

    async def add_address(self, address: Address) -> Address:
        """Persist a new address and return it with a generated id."""
        self.session.add(address)
        await self.session.flush()
        await self.session.refresh(address)
        return address

    async def delete_address(self, address: Address) -> None:
        """Delete an address row."""
        await self.session.delete(address)
        await self.session.flush()
