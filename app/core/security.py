"""Password hashing (argon2) and JWT create/verify for access + refresh tokens (§7).

Tokens are HS256-signed (secret + algorithm from settings). Every token carries:

* ``sub``  — user id (as string, per JWT spec);
* ``jti``  — unique token id (used for refresh revocation / blacklist);
* ``exp``  — expiry (access: minutes, refresh: days — both from settings);
* ``type`` — ``"access"`` or ``"refresh"`` so a refresh token can't be used as
  an access token and vice-versa.

Password hashes are argon2id via :class:`argon2.PasswordHasher`; only the hash is
ever persisted (never the plaintext).
"""

import uuid
from datetime import UTC, datetime, timedelta
from enum import Enum

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from pydantic import BaseModel

from app.core.config import settings

_password_hasher = PasswordHasher()


class TokenType(str, Enum):
    """Discriminates access tokens from refresh tokens."""

    ACCESS = "access"
    REFRESH = "refresh"


class TokenClaims(BaseModel):
    """Decoded and validated JWT claims.

    Attributes:
        sub: User id the token was issued for.
        jti: Unique token identifier (for refresh revocation).
        type: Token type (access or refresh).
        exp: Expiry as a UNIX timestamp.
    """

    sub: int
    jti: str
    type: TokenType
    exp: int


class InvalidTokenError(Exception):
    """Raised when a JWT is malformed, expired, or has the wrong type."""


def hash_password(plain_password: str) -> str:
    """Hash a plaintext password with argon2id.

    Args:
        plain_password: The user-supplied password.

    Returns:
        str: The argon2 hash to persist (never store the plaintext).
    """
    return _password_hasher.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a stored argon2 hash.

    Args:
        plain_password: The candidate password.
        password_hash: The stored argon2 hash.

    Returns:
        bool: ``True`` if the password matches, ``False`` otherwise.
    """
    try:
        return _password_hasher.verify(password_hash, plain_password)
    except VerifyMismatchError:
        return False


def _create_token(
    user_id: int, token_type: TokenType, ttl: timedelta
) -> tuple[str, str]:
    """Encode a signed JWT of the given type.

    Args:
        user_id: The subject user id.
        token_type: Access or refresh.
        ttl: Time-to-live for the token.

    Returns:
        tuple[str, str]: ``(encoded_token, jti)``.
    """
    jti = uuid.uuid4().hex
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "jti": jti,
        "type": token_type.value,
        "exp": now + ttl,
        "iat": now,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, jti


def create_access_token(user_id: int) -> str:
    """Create a signed access token for ``user_id`` (TTL from settings)."""
    ttl = timedelta(minutes=settings.access_token_ttl_min)
    token, _ = _create_token(user_id, TokenType.ACCESS, ttl)
    return token


def create_refresh_token(user_id: int) -> tuple[str, str]:
    """Create a signed refresh token for ``user_id``.

    Returns:
        tuple[str, str]: ``(refresh_token, jti)`` — the ``jti`` is needed to
            blacklist the token on rotation/logout.
    """
    ttl = timedelta(days=settings.refresh_token_ttl_days)
    return _create_token(user_id, TokenType.REFRESH, ttl)


def decode_token(token: str, expected_type: TokenType) -> TokenClaims:
    """Decode and validate a JWT, asserting its type.

    Args:
        token: The encoded JWT.
        expected_type: The token type the caller requires.

    Returns:
        TokenClaims: The validated claims.

    Raises:
        InvalidTokenError: If the token is malformed, expired, signed with the
            wrong key, or is not of ``expected_type``.
    """
    try:
        raw = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("Token could not be decoded") from exc

    if raw.get("type") != expected_type.value:
        raise InvalidTokenError("Unexpected token type")

    try:
        return TokenClaims(
            sub=int(raw["sub"]),
            jti=raw["jti"],
            type=TokenType(raw["type"]),
            exp=raw["exp"],
        )
    except (KeyError, ValueError) as exc:
        raise InvalidTokenError("Token claims are invalid") from exc
