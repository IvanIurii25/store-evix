"""Router-body tests for the admin-settings endpoints (admin §6.4).

The endpoint coroutines are invoked *directly* (not via the ASGI app) so their
full bodies — including the response-model construction on the ``return`` line
and the ``StaffNotFoundError`` / ``StaffConflictError`` propagation — run inside
the traced test coroutine. Going through the ASGI transport runs the endpoint
body in Starlette's worker context, which the coverage tracer does not follow,
so those return lines would otherwise read as missed.

Run with ``EVIX_TEST_DB=evix_test_admin_settings``.
"""

import pytest
from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.admin_settings import create_staff, list_staff, update_staff
from app.schemas.admin_settings import StaffCreate, StaffUpdate
from app.services.admin_settings_service import (
    StaffConflictError,
    StaffNotFoundError,
)
from tests.factories import StaffFactory, persist

pytestmark = pytest.mark.asyncio

_NEW_EMAIL = "router-new@example.com"
_NEW_PASSWORD = "supersecret"
_MISSING_USER_ID = 424242


class TestAdminStaffRouterSuccess:
    """Success bodies of the staff endpoints (list / create returns)."""

    async def test_list_staff_serializes_roster_envelope(
        self, db_session: AsyncSession, staff_user
    ) -> None:
        """``list_staff`` wraps the roster rows into a ``StaffList`` envelope."""
        # Arrange: the seed staff user is present in the roster.
        # (``staff_user`` fixture persisted it.)

        # Act: invoke the endpoint coroutine directly.
        result = await list_staff(_staff=staff_user, session=db_session)

        # Assert: the seed user's email is in the serialized envelope.
        emails = {item.email for item in result.data}
        assert staff_user.email in emails, "roster must include the seed staff"

    async def test_create_staff_returns_serialized_item(
        self, db_session: AsyncSession, staff_user
    ) -> None:
        """``create_staff`` returns the created row as a ``StaffItem``."""
        # Arrange: a valid create payload for a new email.
        payload = StaffCreate(email=_NEW_EMAIL, password=_NEW_PASSWORD)

        # Act: invoke the endpoint coroutine directly.
        item = await create_staff(
            payload=payload, _staff=staff_user, session=db_session
        )

        # Assert: the returned item reflects the created active staff user.
        assert item.email == _NEW_EMAIL, "returned item must carry the new email"
        assert item.is_staff is True, "created user must be staff"


class TestAdminStaffRouterErrors:
    """Domain errors propagated by the ``update_staff`` endpoint (404 / 409)."""

    async def test_update_missing_user_raises_not_found(
        self, db_session: AsyncSession, staff_user
    ) -> None:
        """A missing user id raises a 404 ``StaffNotFoundError`` (``not_found``)."""
        # Arrange: an update targeting a non-existent id.
        payload = StaffUpdate(is_active=False)

        # Act / Assert: the endpoint propagates the domain error.
        with pytest.raises(StaffNotFoundError) as exc_info:
            await update_staff(
                user_id=_MISSING_USER_ID,
                payload=payload,
                _staff=staff_user,
                session=db_session,
            )
        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND, (
            "missing user must carry a 404 status code"
        )
        assert exc_info.value.code == "not_found", (
            "error must carry the not_found code"
        )

    async def test_update_last_active_staff_raises_conflict(
        self, db_session: AsyncSession, staff_user
    ) -> None:
        """Deactivating the last active staff raises a 409 ``StaffConflictError``."""
        # Arrange: the seed ``staff_user`` is the only active staff account.
        payload = StaffUpdate(is_active=False)

        # Act / Assert: the endpoint propagates the domain error.
        with pytest.raises(StaffConflictError) as exc_info:
            await update_staff(
                user_id=staff_user.id,
                payload=payload,
                _staff=staff_user,
                session=db_session,
            )
        assert exc_info.value.status_code == status.HTTP_409_CONFLICT, (
            "last-active-staff removal must carry a 409 status code"
        )
        assert exc_info.value.code == "conflict", (
            "error must carry the conflict code"
        )

    async def test_update_non_last_staff_returns_serialized_item(
        self, db_session: AsyncSession, staff_user
    ) -> None:
        """A guard-free update returns the updated row as a ``StaffItem``."""
        # Arrange: a second staff user so the seed stays a way in.
        target = await persist(db_session, StaffFactory())
        payload = StaffUpdate(is_active=False)

        # Act: deactivate the non-last staff via the endpoint coroutine.
        item = await update_staff(
            user_id=target.id,
            payload=payload,
            _staff=staff_user,
            session=db_session,
        )

        # Assert: the returned item reflects the deactivation.
        assert item.id == target.id, "returned item must be the updated row"
        assert item.is_active is False, "returned item must be deactivated"
