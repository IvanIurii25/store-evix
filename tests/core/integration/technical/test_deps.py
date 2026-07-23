"""Unit tests for the auth dependencies in :mod:`app.api.deps`.

Each dependency function is invoked directly (no ASGI round-trip) with a crafted
``Request`` and a real :class:`AuthService` bound to the transactional test
session + isolated Redis, so the token-extraction and user-resolution branches
are exercised in isolation.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException, status

from app.api.deps import (
    _extract_access_token,
    current_staff,
    current_user,
    get_auth_service,
    guest_or_user,
)
from app.core.security import create_access_token
from app.services.auth_service import AuthService
from tests.core._helpers import build_request
from tests.factories import create_staff, create_user

# Named constants for the token-shape branches under test.
_MALFORMED_TOKEN = "not-a-real-jwt"
_UNKNOWN_USER_ID = 999_999_999


def _service(session, redis) -> AuthService:
    """Build an :class:`AuthService` bound to the given session + redis."""
    return AuthService(session=session, redis=redis)


class TestDepsExtractToken:
    """``_extract_access_token`` header/cookie precedence branches."""

    def test_extract_bearer_header_returns_stripped_token(self):
        # Arrange: a request carrying a Bearer authorization header.
        request = build_request(headers={"Authorization": "Bearer abc123"})

        # Act: extract the token.
        token = _extract_access_token(request)

        # Assert: the raw token (header prefix stripped) is returned.
        assert token == "abc123", "Bearer prefix must be stripped from the header"

    def test_extract_cookie_used_when_no_header_returns_cookie(self):
        # Arrange: no Authorization header, an ``access`` cookie instead.
        request = build_request(cookies={"access": "cookie-token"})

        # Act.
        token = _extract_access_token(request)

        # Assert: the cookie value is used as the fallback source.
        assert token == "cookie-token", "cookie must be the fallback token source"

    def test_extract_no_source_returns_none(self):
        # Arrange: neither header nor cookie present.
        request = build_request()

        # Act.
        token = _extract_access_token(request)

        # Assert: absence of both sources yields ``None``.
        assert token is None, "absent header and cookie must yield None"


class TestDepsCurrentUser:
    """``current_user`` — required auth: success + every 401 branch."""

    async def test_current_user_valid_token_returns_user(
        self, db_session, redis_client
    ):
        # Arrange: a real user and a valid access token in the Bearer header.
        user = await create_user(db_session)
        token = create_access_token(user.id)
        request = build_request(headers={"Authorization": f"Bearer {token}"})

        # Act: resolve the authenticated user.
        resolved = await current_user(request, _service(db_session, redis_client))

        # Assert: the same user is returned.
        assert resolved.id == user.id, "valid token must resolve to its user"

    async def test_current_user_missing_token_raises_401(
        self, db_session, redis_client
    ):
        # Arrange: a request with no token at all.
        request = build_request()

        # Act / Assert: a missing token raises 401.
        with pytest.raises(HTTPException) as exc_info:
            await current_user(request, _service(db_session, redis_client))
        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED, (
            "a missing token must raise 401"
        )

    async def test_current_user_invalid_token_raises_401(
        self, db_session, redis_client
    ):
        # Arrange: a syntactically broken token.
        request = build_request(headers={"Authorization": f"Bearer {_MALFORMED_TOKEN}"})

        # Act / Assert: an undecodable token raises 401.
        with pytest.raises(HTTPException) as exc_info:
            await current_user(request, _service(db_session, redis_client))
        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED, (
            "an invalid token must raise 401"
        )

    async def test_current_user_unknown_subject_raises_401(
        self, db_session, redis_client
    ):
        # Arrange: a well-formed token for a user id that does not exist.
        token = create_access_token(_UNKNOWN_USER_ID)
        request = build_request(headers={"Authorization": f"Bearer {token}"})

        # Act / Assert: a token whose subject is missing raises 401.
        with pytest.raises(HTTPException) as exc_info:
            await current_user(request, _service(db_session, redis_client))
        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED, (
            "an unknown user must raise 401"
        )

    async def test_current_user_inactive_user_raises_401(
        self, db_session, redis_client
    ):
        # Arrange: an existing but deactivated user with a valid token.
        user = await create_user(db_session, is_active=False)
        token = create_access_token(user.id)
        request = build_request(headers={"Authorization": f"Bearer {token}"})

        # Act / Assert: an inactive user must not authenticate.
        with pytest.raises(HTTPException) as exc_info:
            await current_user(request, _service(db_session, redis_client))
        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED, (
            "an inactive user must raise 401"
        )


class TestDepsStaffAndGuest:
    """``current_staff`` (403 gate) and ``guest_or_user`` (never-raise) branches."""

    async def test_current_staff_staff_user_returns_user(self, db_session):
        # Arrange: a staff user already resolved by ``current_user``.
        staff = await create_staff(db_session)

        # Act: apply the staff gate directly.
        resolved = await current_staff(staff)

        # Assert: a staff user passes through unchanged.
        assert resolved.id == staff.id, "a staff user must pass the staff gate"

    async def test_current_staff_non_staff_raises_403(self, db_session):
        # Arrange: an ordinary (non-staff) user.
        user = await create_user(db_session)

        # Act / Assert: a non-staff user is forbidden.
        with pytest.raises(HTTPException) as exc_info:
            await current_staff(user)
        assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN, (
            "a non-staff user must raise 403"
        )

    async def test_guest_or_user_valid_token_returns_user(
        self, db_session, redis_client
    ):
        # Arrange: a valid token for a real user.
        user = await create_user(db_session)
        token = create_access_token(user.id)
        request = build_request(headers={"Authorization": f"Bearer {token}"})

        # Act.
        resolved = await guest_or_user(request, _service(db_session, redis_client))

        # Assert: a valid token resolves the user.
        assert resolved is not None and resolved.id == user.id, (
            "valid token must resolve the optional user"
        )

    async def test_guest_or_user_no_token_returns_none(self, db_session, redis_client):
        # Arrange: an anonymous request (no token).
        request = build_request()

        # Act.
        resolved = await guest_or_user(request, _service(db_session, redis_client))

        # Assert: anonymous callers get ``None`` (no raise).
        assert resolved is None, "a missing token must yield None, not raise"

    async def test_guest_or_user_invalid_token_returns_none(
        self, db_session, redis_client
    ):
        # Arrange: a broken token.
        request = build_request(headers={"Authorization": f"Bearer {_MALFORMED_TOKEN}"})

        # Act.
        resolved = await guest_or_user(request, _service(db_session, redis_client))

        # Assert: an invalid token is swallowed, yielding ``None``.
        assert resolved is None, "an invalid token must yield None, not raise"

    async def test_guest_or_user_inactive_user_returns_none(
        self, db_session, redis_client
    ):
        # Arrange: a valid token for a deactivated user.
        user = await create_user(db_session, is_active=False)
        token = create_access_token(user.id)
        request = build_request(headers={"Authorization": f"Bearer {token}"})

        # Act.
        resolved = await guest_or_user(request, _service(db_session, redis_client))

        # Assert: an inactive user resolves to ``None``.
        assert resolved is None, "an inactive user must yield None"


class TestDepsAuthServiceFactory:
    """``get_auth_service`` wires the session + redis into an ``AuthService``."""

    def test_get_auth_service_builds_bound_service(self, db_session, redis_client):
        # Arrange / Act: build the service via the factory dependency.
        service = get_auth_service(session=db_session, redis=redis_client)

        # Assert: the returned service is bound to the given collaborators.
        assert isinstance(service, AuthService), "factory must return an AuthService"
        assert service.session is db_session, "service must bind the given session"
        assert service.redis is redis_client, "service must bind the given redis"
