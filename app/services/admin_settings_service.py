"""Back-office settings services: SEO defaults + staff management (§6.4).

Two small, focused services bound to one session:

* :class:`SettingsService` — reads/writes the site-wide SEO defaults, mapping the
  typed :class:`~app.schemas.admin_settings.SeoSettings` block to/from
  ``site_setting`` key/value rows (keys ``seo.<field>``).
* :class:`StaffService` — the staff roster: list, create-or-promote (by email),
  and (de)activate / toggle the ``is_staff`` flag. A guard refuses to remove the
  last active staff account so the back office can never lock itself out.

No HTTP knowledge here — the domain errors below subclass
:class:`~app.core.errors.DomainError`, carrying the ``status_code`` and ``code``
the unified error envelope renders; the router just lets them propagate.
"""

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.security import hash_password
from app.models.user import AppUser
from app.repositories.admin_staff_repo import AdminStaffRepository
from app.repositories.settings_repo import SettingsRepository
from app.schemas.admin_settings import SeoSettings

# The SEO fields persisted under the ``seo.<field>`` key namespace (§6.4).
_SEO_FIELDS: tuple[str, ...] = (
    "title_ru",
    "title_ro",
    "description_ru",
    "description_ro",
    "title_suffix",
    "og_image_url",
)


def _seo_key(field: str) -> str:
    """Return the ``site_setting`` key for a SEO field."""
    return f"seo.{field}"


class SettingsError(DomainError):
    """Base class for settings domain errors (rendered via the unified envelope)."""

    code: str = "settings_error"


class StaffNotFoundError(SettingsError):
    """The referenced staff user does not exist (404)."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class StaffConflictError(SettingsError):
    """A staff write violates an invariant (duplicate / last-staff, 409)."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class SettingsService:
    """Read/write the site-wide SEO defaults (§6.4)."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and its settings repository.

        Args:
            session: Active async session (request- or test-scoped).
        """
        self.session = session
        self.repo = SettingsRepository(session)

    async def get_seo(self) -> SeoSettings:
        """Return the current SEO defaults (missing keys default to empty).

        Returns:
            SeoSettings: A fully-formed block even on a fresh install.
        """
        stored = await self.repo.get_map([_seo_key(f) for f in _SEO_FIELDS])
        return SeoSettings(
            **{field: stored.get(_seo_key(field), "") for field in _SEO_FIELDS}
        )

    async def put_seo(self, data: SeoSettings) -> SeoSettings:
        """Persist the SEO defaults and return the stored block.

        Args:
            data: The new SEO defaults (all fields).

        Returns:
            SeoSettings: The persisted block.
        """
        for field in _SEO_FIELDS:
            await self.repo.upsert(_seo_key(field), getattr(data, field))
        await self.session.commit()
        return await self.get_seo()


class StaffService:
    """Manage the staff roster: list / create-or-promote / (de)activate (§6.4)."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and its staff repository.

        Args:
            session: Active async session (request- or test-scoped).
        """
        self.session = session
        self.repo = AdminStaffRepository(session)

    async def list_staff(self) -> list[AppUser]:
        """Return the staff roster (newest first)."""
        return await self.repo.list_staff()

    async def create_or_promote(
        self,
        email: str,
        password: str,
        phone: str | None,
    ) -> AppUser:
        """Create a new active staff user, or promote + reset an existing one.

        Args:
            email: Login email; if it already exists, that user is promoted to
                staff, reactivated, and its password reset.
            password: Plaintext password (hashed with argon2 before storage).
            phone: Optional phone; set only when provided.

        Returns:
            AppUser: The created or promoted staff user.
        """
        existing = await self.repo.get_by_email(email)
        if existing is None:
            user = AppUser(
                email=email,
                password_hash=hash_password(password),
                phone=phone,
                is_active=True,
                is_staff=True,
            )
            await self.repo.add(user)
        else:
            existing.is_staff = True
            existing.is_active = True
            existing.password_hash = hash_password(password)
            if phone is not None:
                existing.phone = phone
            user = existing
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def update_staff(
        self,
        user_id: int,
        *,
        is_active: bool | None,
        is_staff: bool | None,
    ) -> AppUser:
        """Toggle a staff user's activation / staff flag with a lockout guard.

        Removing staff rights or deactivating the *last* remaining active staff
        account is refused — the back office must always keep at least one way in.

        Args:
            user_id: The user to update.
            is_active: New activation state, or ``None`` to leave unchanged.
            is_staff: New staff flag, or ``None`` to leave unchanged.

        Returns:
            AppUser: The updated user.

        Raises:
            StaffNotFoundError: If the user does not exist.
            StaffConflictError: If the change would remove the last active staff.
        """
        user = await self.repo.get(user_id)
        if user is None:
            raise StaffNotFoundError(f"Staff user not found: {user_id}")

        would_lose_access = (is_staff is False) or (is_active is False)
        if would_lose_access and await self._is_last_active_staff(user):
            raise StaffConflictError("Cannot remove the last active staff account")

        if is_active is not None:
            user.is_active = is_active
        if is_staff is not None:
            user.is_staff = is_staff
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def _is_last_active_staff(self, user: AppUser) -> bool:
        """Return whether ``user`` is the only currently active staff account."""
        active_staff = [s for s in await self.repo.list_staff() if s.is_active]
        return active_staff == [user] or (
            len(active_staff) == 1 and active_staff[0].id == user.id
        )
