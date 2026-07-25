"""Auth routes: register, login, refresh, logout (§4, §7).

Login sets httpOnly ``access``/``refresh`` cookies (``Secure``, ``SameSite=Lax``
for Astro SSR-guard) **and** returns the pair in the body. Refresh rotates the
token; logout blacklists it. The router carries no ``/api/v1`` prefix (mounted by
``main.py``).
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.deps import get_auth_service
from app.api.ratelimit import rate_limiter
from app.core.config import settings
from app.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserMe,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])

_ACCESS_MAX_AGE = settings.access_token_ttl_min * 60
_REFRESH_MAX_AGE = settings.refresh_token_ttl_days * 24 * 60 * 60


def _set_auth_cookies(response: Response, tokens: TokenPair) -> None:
    """Write access + refresh as httpOnly cookies (Secure, SameSite=Lax).

    Args:
        response: The response whose cookies are set.
        tokens: The token pair to store.
    """
    response.set_cookie(
        key="access",
        value=tokens.access,
        max_age=_ACCESS_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        key="refresh",
        value=tokens.refresh,
        max_age=_REFRESH_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _read_refresh(request: Request, body: RefreshRequest | None) -> str:
    """Resolve the refresh token from the body or the ``refresh`` cookie.

    Args:
        request: The incoming request (cookie fallback).
        body: Optional request body carrying the token.

    Returns:
        str: The refresh token.

    Raises:
        HTTPException: 401 if no refresh token is present.
    """
    token = (body.refresh if body else None) or request.cookies.get("refresh")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing refresh token",
        )
    return token


@router.post(
    "/register",
    response_model=UserMe,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limiter(settings.rate_limit_register, "register"))],
)
async def register(
    data: RegisterRequest,
    service: AuthService = Depends(get_auth_service),
) -> UserMe:
    """Register a new user (duplicate email/phone -> 409)."""
    user = await service.register(data)
    return UserMe.model_validate(user)


@router.post(
    "/login",
    response_model=TokenPair,
    dependencies=[Depends(rate_limiter(settings.rate_limit_login, "login"))],
)
async def login(
    data: LoginRequest,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> TokenPair:
    """Authenticate, set auth cookies, and return the token pair."""
    tokens = await service.login(data)
    _set_auth_cookies(response, tokens)
    return tokens


@router.post(
    "/refresh",
    response_model=TokenPair,
    dependencies=[Depends(rate_limiter(settings.rate_limit_refresh, "refresh"))],
)
async def refresh(
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),
    body: RefreshRequest | None = None,
) -> TokenPair:
    """Rotate the refresh token and issue a new pair (revoked/invalid -> 401)."""
    token = _read_refresh(request, body)
    tokens = await service.refresh(token)
    _set_auth_cookies(response, tokens)
    return tokens


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),
    body: RefreshRequest | None = None,
) -> Response:
    """Blacklist the refresh token and clear auth cookies."""
    token = _read_refresh(request, body)
    await service.logout(token)
    response.delete_cookie("access", path="/")
    response.delete_cookie("refresh", path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
