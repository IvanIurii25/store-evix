"""Auth dependencies: current user (required / staff / optional) (§7).

An access token is read from the ``Authorization: Bearer`` header OR the
``access`` cookie (cookie-first strategy supports Astro SSR-guard, §7). Invalid
or missing tokens yield 401 for the required dependencies and ``None`` for the
optional one.
"""

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.redis import get_redis
from app.core.security import InvalidTokenError, TokenType, decode_token
from app.models.user import AppUser
from app.services.auth_service import AuthService


def get_auth_service(
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> AuthService:
    """Construct an :class:`AuthService` bound to the request session + Redis."""
    return AuthService(session=session, redis=redis)


def _extract_access_token(request: Request) -> str | None:
    """Pull the access token from the Bearer header or the ``access`` cookie.

    Args:
        request: The incoming request.

    Returns:
        str | None: The raw token, or ``None`` if neither source is present.
    """
    header = request.headers.get("Authorization")
    if header and header.lower().startswith("bearer "):
        return header[len("bearer ") :].strip()
    return request.cookies.get("access")


async def current_user(
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> AppUser:
    """Resolve and return the authenticated user, or raise 401.

    Args:
        request: The incoming request (source of the token).
        service: Injected auth service (loads the user).

    Returns:
        AppUser: The authenticated, active user.

    Raises:
        HTTPException: 401 if the token is missing, invalid, or the user is gone
            / inactive.
    """
    token = _extract_access_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    try:
        claims = decode_token(token, TokenType.ACCESS)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token",
        ) from exc

    user = await service.repo.get_by_id(claims.sub)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


async def current_staff(user: AppUser = Depends(current_user)) -> AppUser:
    """Require the current user to be staff, else raise 403.

    Args:
        user: The authenticated user.

    Returns:
        AppUser: The user, guaranteed ``is_staff``.

    Raises:
        HTTPException: 403 if the user is not staff.
    """
    if not user.is_staff:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Staff access required",
        )
    return user


async def guest_or_user(
    request: Request,
    service: AuthService = Depends(get_auth_service),
) -> AppUser | None:
    """Return the authenticated user if a valid token is present, else ``None``.

    Never raises on a missing/invalid token — used for endpoints open to guests.

    Args:
        request: The incoming request.
        service: Injected auth service.

    Returns:
        AppUser | None: The user, or ``None`` for an anonymous caller.
    """
    token = _extract_access_token(request)
    if not token:
        return None
    try:
        claims = decode_token(token, TokenType.ACCESS)
    except InvalidTokenError:
        return None
    user = await service.repo.get_by_id(claims.sub)
    if user is None or not user.is_active:
        return None
    return user
