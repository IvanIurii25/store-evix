"""Direct service-layer tests for :class:`AuthService` (register/login/token flows).

These complement the API-level tests in ``test_auth_api.py`` by driving the
service directly, covering the error branches that the happy-path API tests do
not reach (bad credentials, inactive user, duplicate phone, invalid/expired/
revoked refresh tokens, and the expired-blacklist no-op).
"""

import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import TokenType, create_refresh_token, decode_token
from app.schemas.auth import LoginRequest, RegisterRequest
from app.services.auth_service import _BLACKLIST_PREFIX, AuthService
from tests.factories import TEST_PASSWORD, create_user

pytestmark = pytest.mark.asyncio

_WRONG_PASSWORD = "definitely-not-the-password"
_NEW_EMAIL = "fresh@example.com"
_NEW_PHONE = "+37361111111"


def _encode_refresh(user_id: int, jti: str, exp: datetime) -> str:
    """Hand-craft a refresh JWT with an explicit ``exp`` (for expiry tests)."""
    payload = {
        "sub": str(user_id),
        "jti": jti,
        "type": TokenType.REFRESH.value,
        "exp": exp,
        "iat": datetime.now(UTC),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


class TestAuthRegister:
    """Registration success + duplicate-email/phone conflicts."""

    async def test_register_new_user_persists_and_hashes(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        service = AuthService(session=tx_session, redis=redis_client)
        data = RegisterRequest(
            email=_NEW_EMAIL, password=TEST_PASSWORD, phone=_NEW_PHONE
        )

        # Act
        user = await service.register(data)

        # Assert
        assert user.id is not None, "registered user must be persisted with an id"
        assert user.email == _NEW_EMAIL, "email must round-trip"
        assert user.password_hash != TEST_PASSWORD, "plaintext must never be stored"

    async def test_register_duplicate_email_raises_conflict(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        existing = await create_user(tx_session, email="taken@example.com")
        service = AuthService(session=tx_session, redis=redis_client)
        data = RegisterRequest(email=existing.email, password=TEST_PASSWORD)

        # Act / Assert
        with pytest.raises(Exception) as exc_info:
            await service.register(data)
        assert exc_info.value.status_code == 409, "duplicate email must be 409"

    async def test_register_duplicate_phone_raises_conflict(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        existing = await create_user(
            tx_session, email="hasphone@example.com", phone=_NEW_PHONE
        )
        service = AuthService(session=tx_session, redis=redis_client)
        data = RegisterRequest(
            email="other@example.com", password=TEST_PASSWORD, phone=existing.phone
        )

        # Act / Assert
        with pytest.raises(Exception) as exc_info:
            await service.register(data)
        assert exc_info.value.status_code == 409, "duplicate phone must be 409"


class TestAuthLogin:
    """Login success + the three 401 failure branches."""

    async def test_login_valid_credentials_issues_pair(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        user = await create_user(tx_session, email="ok@example.com")
        service = AuthService(session=tx_session, redis=redis_client)
        data = LoginRequest(email=user.email, password=TEST_PASSWORD)

        # Act
        pair = await service.login(data)

        # Assert
        claims = decode_token(pair.refresh, TokenType.REFRESH)
        assert claims.sub == user.id, "refresh token must be minted for the user"
        assert pair.access, "an access token must be issued"

    async def test_login_wrong_password_raises_401(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        user = await create_user(tx_session, email="wrongpw@example.com")
        service = AuthService(session=tx_session, redis=redis_client)
        data = LoginRequest(email=user.email, password=_WRONG_PASSWORD)

        # Act / Assert
        with pytest.raises(Exception) as exc_info:
            await service.login(data)
        assert exc_info.value.status_code == 401, "bad password must be 401"

    async def test_login_unknown_email_raises_401(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        service = AuthService(session=tx_session, redis=redis_client)
        data = LoginRequest(email="ghost@example.com", password=TEST_PASSWORD)

        # Act / Assert
        with pytest.raises(Exception) as exc_info:
            await service.login(data)
        assert exc_info.value.status_code == 401, "unknown email must be 401"

    async def test_login_inactive_user_raises_401(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        user = await create_user(
            tx_session, email="inactive@example.com", is_active=False
        )
        service = AuthService(session=tx_session, redis=redis_client)
        data = LoginRequest(email=user.email, password=TEST_PASSWORD)

        # Act / Assert
        with pytest.raises(Exception) as exc_info:
            await service.login(data)
        assert exc_info.value.status_code == 401, "inactive account must be 401"


class TestAuthTokenFlow:
    """Refresh rotation + revocation + logout blacklist branches."""

    async def test_refresh_valid_token_rotates_pair(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        user = await create_user(tx_session, email="rot@example.com")
        service = AuthService(session=tx_session, redis=redis_client)
        old_refresh, old_jti = create_refresh_token(user.id)

        # Act
        new_pair = await service.refresh(old_refresh)

        # Assert
        assert new_pair.refresh != old_refresh, "refresh must rotate to a new token"
        blacklisted = await redis_client.exists(f"{_BLACKLIST_PREFIX}{old_jti}")
        assert blacklisted == 1, "the rotated-out jti must be blacklisted"

    async def test_refresh_invalid_token_raises_401(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        service = AuthService(session=tx_session, redis=redis_client)

        # Act / Assert
        with pytest.raises(Exception) as exc_info:
            await service.refresh("not-a-jwt")
        assert exc_info.value.status_code == 401, "garbage token must be 401"

    async def test_refresh_expired_token_raises_401(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        service = AuthService(session=tx_session, redis=redis_client)
        expired = _encode_refresh(
            user_id=1,
            jti=uuid.uuid4().hex,
            exp=datetime.now(UTC) - timedelta(minutes=5),
        )

        # Act / Assert
        with pytest.raises(Exception) as exc_info:
            await service.refresh(expired)
        assert exc_info.value.status_code == 401, "expired token must be 401"

    async def test_refresh_revoked_token_raises_401(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        user = await create_user(tx_session, email="revoked@example.com")
        service = AuthService(session=tx_session, redis=redis_client)
        refresh, jti = create_refresh_token(user.id)
        await redis_client.set(f"{_BLACKLIST_PREFIX}{jti}", "1")

        # Act / Assert
        with pytest.raises(Exception) as exc_info:
            await service.refresh(refresh)
        assert exc_info.value.status_code == 401, "revoked token must be 401"

    async def test_logout_blacklists_refresh_jti(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        user = await create_user(tx_session, email="lo@example.com")
        service = AuthService(session=tx_session, redis=redis_client)
        refresh, jti = create_refresh_token(user.id)

        # Act
        await service.logout(refresh)

        # Assert
        blacklisted = await redis_client.exists(f"{_BLACKLIST_PREFIX}{jti}")
        assert blacklisted == 1, "logout must blacklist the refresh jti"

    async def test_blacklist_past_exp_is_noop(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange: _blacklist guards ttl<=0 (a jti already past its expiry). This
        # is only reachable via a token whose exp elapses between decode and the
        # blacklist write, so we drive the branch directly on the real service.
        service = AuthService(session=tx_session, redis=redis_client)
        jti = uuid.uuid4().hex
        past_exp = int(time.time()) - 1

        # Act
        await service._blacklist(jti, past_exp)

        # Assert
        blacklisted = await redis_client.exists(f"{_BLACKLIST_PREFIX}{jti}")
        assert blacklisted == 0, "an already-expired jti must not be stored"
