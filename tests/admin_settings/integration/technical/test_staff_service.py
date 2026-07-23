"""Direct service tests for :class:`StaffService` (admin §6.4).

Bypasses HTTP to exercise the roster branches: create a brand-new staff user,
promote-and-reset an existing user (with and without a phone), the not-found
guard, plain (de)activation / flag toggles, and the last-active-staff lockout
guard (both the single-account refusal and the multi-account allow path).

Run with ``EVIX_TEST_DB=evix_test_admin_settings``.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_password
from app.services.admin_settings_service import (
    StaffConflictError,
    StaffNotFoundError,
    StaffService,
)
from tests.factories import StaffFactory, UserFactory, persist

pytestmark = pytest.mark.asyncio

_NEW_EMAIL = "brand-new@example.com"
_NEW_PASSWORD = "supersecret"
_NEW_PHONE = "+37360000001"
_MISSING_USER_ID = 987654


class TestStaffServiceCreateOrPromote:
    """Branches of :meth:`StaffService.create_or_promote`."""

    async def test_create_new_user_returns_active_staff_with_hash(
        self, db_session: AsyncSession
    ) -> None:
        """An unknown email creates an active staff user with a hashed password."""
        # Arrange: no user with the email exists.
        service = StaffService(db_session)

        # Act: create the staff user.
        user = await service.create_or_promote(
            email=_NEW_EMAIL, password=_NEW_PASSWORD, phone=_NEW_PHONE
        )

        # Assert: active staff, phone set, password stored hashed (verifiable).
        assert user.is_staff is True, "new user must be staff"
        assert user.is_active is True, "new user must be active"
        assert user.phone == _NEW_PHONE, "phone must be set when provided"
        assert verify_password(_NEW_PASSWORD, user.password_hash) is True, (
            "password must be stored as a verifiable hash"
        )

    async def test_promote_existing_user_reactivates_and_resets(
        self, db_session: AsyncSession
    ) -> None:
        """A known email promotes + reactivates the row and resets its password."""
        # Arrange: an inactive non-staff user already exists.
        existing = await persist(
            db_session,
            UserFactory(email=_NEW_EMAIL, is_staff=False, is_active=False),
        )
        service = StaffService(db_session)

        # Act: post the same email to promote it.
        user = await service.create_or_promote(
            email=_NEW_EMAIL, password=_NEW_PASSWORD, phone=_NEW_PHONE
        )

        # Assert: the same row is now active staff with the new password.
        assert user.id == existing.id, "promotion must reuse the existing row"
        assert user.is_staff is True, "promoted user must be staff"
        assert user.is_active is True, "promoted user must be reactivated"
        assert verify_password(_NEW_PASSWORD, user.password_hash) is True, (
            "password must be reset to the new value"
        )

    async def test_promote_existing_without_phone_keeps_prior_phone(
        self, db_session: AsyncSession
    ) -> None:
        """Promoting with ``phone=None`` leaves the existing phone untouched."""
        # Arrange: an existing user that already has a phone.
        prior_phone = "+37360000099"
        await persist(
            db_session,
            UserFactory(email=_NEW_EMAIL, phone=prior_phone, is_staff=False),
        )
        service = StaffService(db_session)

        # Act: promote without supplying a phone.
        user = await service.create_or_promote(
            email=_NEW_EMAIL, password=_NEW_PASSWORD, phone=None
        )

        # Assert: the prior phone survives the promotion.
        assert user.phone == prior_phone, "None phone must leave the phone unchanged"


class TestStaffServiceUpdateStaff:
    """Branches of :meth:`StaffService.update_staff` incl. the lockout guard."""

    async def test_update_unknown_user_raises_not_found(
        self, db_session: AsyncSession
    ) -> None:
        """Updating a missing user id raises :class:`StaffNotFoundError`."""
        # Arrange: a service with no such user.
        service = StaffService(db_session)

        # Act / Assert: the not-found guard fires.
        with pytest.raises(StaffNotFoundError):
            await service.update_staff(_MISSING_USER_ID, is_active=False, is_staff=None)

    async def test_toggle_flags_on_non_last_staff_applies_changes(
        self, db_session: AsyncSession
    ) -> None:
        """With another active staff present, flags update freely (no guard)."""
        # Arrange: two active staff — deactivating one keeps a way in.
        keep = await persist(db_session, StaffFactory())
        target = await persist(db_session, StaffFactory())
        assert keep.id != target.id  # sanity: distinct rows
        service = StaffService(db_session)

        # Act: deactivate and demote the second staff user.
        updated = await service.update_staff(target.id, is_active=False, is_staff=False)

        # Assert: both flags applied, guard did not trip.
        assert updated.is_active is False, "is_active must be updated"
        assert updated.is_staff is False, "is_staff must be updated"

    async def test_deactivate_last_active_staff_raises_conflict(
        self, db_session: AsyncSession
    ) -> None:
        """Deactivating the only active staff account is refused with conflict."""
        # Arrange: exactly one active staff account exists.
        only = await persist(db_session, StaffFactory())
        service = StaffService(db_session)

        # Act / Assert: the lockout guard raises a conflict.
        with pytest.raises(StaffConflictError):
            await service.update_staff(only.id, is_active=False, is_staff=None)

    async def test_demote_last_active_staff_raises_conflict(
        self, db_session: AsyncSession
    ) -> None:
        """Removing the staff flag from the only active staff is also refused."""
        # Arrange: a single active staff account.
        only = await persist(db_session, StaffFactory())
        service = StaffService(db_session)

        # Act / Assert: demoting the last staff trips the guard.
        with pytest.raises(StaffConflictError):
            await service.update_staff(only.id, is_active=None, is_staff=False)

    async def test_toggle_only_staff_flag_leaves_active_unchanged(
        self, db_session: AsyncSession
    ) -> None:
        """With ``is_active=None`` only the staff flag is touched (active kept)."""
        # Arrange: two active staff so the guard stays clear.
        await persist(db_session, StaffFactory())
        target = await persist(db_session, StaffFactory(is_active=True))
        service = StaffService(db_session)

        # Act: re-set the staff flag while leaving activation untouched.
        updated = await service.update_staff(target.id, is_active=None, is_staff=True)

        # Assert: staff flag applied, activation left as it was.
        assert updated.is_staff is True, "staff flag must be applied"
        assert updated.is_active is True, "None is_active must leave activation as-is"

    async def test_update_last_staff_with_safe_change_is_allowed(
        self, db_session: AsyncSession
    ) -> None:
        """A change that does not remove access is allowed for the last staff."""
        # Arrange: a single active staff account.
        only = await persist(db_session, StaffFactory())
        service = StaffService(db_session)

        # Act: re-affirm active + staff (would_lose_access is False).
        updated = await service.update_staff(only.id, is_active=True, is_staff=True)

        # Assert: no guard, flags stay set.
        assert updated.is_active is True, "safe change must be applied"
        assert updated.is_staff is True, "staff flag must remain set"
