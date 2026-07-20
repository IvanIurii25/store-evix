"""Cart endpoints (§4).

Thin router: it resolves the caller (authenticated user via ``guest_or_user``, or
a guest via the ``session_token`` cookie), delegates to
:class:`~app.services.cart_service.CartService`, serializes the result, and maps
domain errors to HTTP status codes (the app's HTTPException handler renders the
unified ``{error:{code,message,details?}}`` envelope). No business logic and no
SQL live here.

Guest identity (§2.3): a guest is tracked by an opaque ``session_token`` cookie.
On the first write from a cookie-less guest we mint a token and set it on the
response so the cart survives subsequent requests. Authenticated callers ignore
the cookie. ``POST /cart/merge`` folds the guest cart into the user cart on login
(§7) and requires authentication.

Mounted without the ``/api/v1`` prefix — the integrator adds it.
"""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, guest_or_user
from app.core.db import get_session
from app.models.user import AppUser
from app.schemas.cart import AddItem, CartOut, UpdateItem
from app.services.cart_service import (
    CartService,
    ItemNotFoundError,
    ProductNotAvailableError,
)

router = APIRouter(prefix="/cart", tags=["cart"])

# Cookie name carrying the guest cart token (§2.3).
SESSION_COOKIE: str = "session_token"
# ~30 days, matching the guest-cart lifetime expectation of §2.3.
_COOKIE_MAX_AGE: int = 60 * 60 * 24 * 30


def _read_session_token(request: Request) -> UUID | None:
    """Parse the guest ``session_token`` cookie into a UUID, if present/valid.

    Args:
        request: The incoming request.

    Returns:
        UUID | None: The token, or ``None`` when absent or malformed.
    """
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def _set_session_cookie(response: Response, token: UUID) -> None:
    """Attach the guest ``session_token`` cookie to the response.

    Args:
        response: The outgoing response.
        token: The token to persist on the client.
    """
    response.set_cookie(
        key=SESSION_COOKIE,
        value=str(token),
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


@router.get("", response_model=CartOut)
async def get_cart(
    request: Request,
    user: AppUser | None = Depends(guest_or_user),
    session: AsyncSession = Depends(get_session),
) -> CartOut:
    """Return the current cart with prices/totals recomputed live (§2.3).

    Args:
        request: Incoming request (source of the guest cookie).
        user: The authenticated user, or ``None`` for a guest.
        session: Injected async DB session.

    Returns:
        CartOut: The live-priced cart (empty when the caller has no cart).
    """
    user_id = user.id if user is not None else None
    token = None if user is not None else _read_session_token(request)
    service = CartService(session)
    return await service.get_cart(user_id, token)


@router.post("/items", response_model=CartOut, status_code=status.HTTP_201_CREATED)
async def add_item(
    data: AddItem,
    request: Request,
    response: Response,
    user: AppUser | None = Depends(guest_or_user),
    session: AsyncSession = Depends(get_session),
) -> CartOut:
    """Add a product to the cart (increments ``qty`` if already present, §2.3).

    A cookie-less guest is issued a fresh ``session_token`` cookie here.

    Args:
        data: The product id + quantity to add.
        request: Incoming request (guest cookie).
        response: Outgoing response (to set the guest cookie).
        user: The authenticated user, or ``None`` for a guest.
        session: Injected async DB session.

    Returns:
        CartOut: The re-rendered cart.

    Raises:
        HTTPException: 404 if the product is missing or inactive.
    """
    user_id = user.id if user is not None else None
    token: UUID | None = None
    if user is None:
        token = _read_session_token(request)
        if token is None:
            token = uuid4()
        _set_session_cookie(response, token)
    service = CartService(session)
    try:
        return await service.add_item(user_id, token, data.product_id, data.qty)
    except ProductNotAvailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@router.patch("/items/{product_id}", response_model=CartOut)
async def update_item(
    product_id: int,
    data: UpdateItem,
    request: Request,
    user: AppUser | None = Depends(guest_or_user),
    session: AsyncSession = Depends(get_session),
) -> CartOut:
    """Set an absolute quantity on an existing cart line.

    Args:
        product_id: The product whose line is being changed.
        data: The new absolute quantity.
        request: Incoming request (guest cookie).
        user: The authenticated user, or ``None`` for a guest.
        session: Injected async DB session.

    Returns:
        CartOut: The re-rendered cart.

    Raises:
        HTTPException: 404 if the cart or line does not exist.
    """
    user_id = user.id if user is not None else None
    token = None if user is not None else _read_session_token(request)
    service = CartService(session)
    try:
        return await service.update_item(user_id, token, product_id, data.qty)
    except ItemNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@router.delete("/items/{product_id}", response_model=CartOut)
async def remove_item(
    product_id: int,
    request: Request,
    user: AppUser | None = Depends(guest_or_user),
    session: AsyncSession = Depends(get_session),
) -> CartOut:
    """Remove a line from the cart.

    Args:
        product_id: The product whose line is removed.
        request: Incoming request (guest cookie).
        user: The authenticated user, or ``None`` for a guest.
        session: Injected async DB session.

    Returns:
        CartOut: The re-rendered cart.

    Raises:
        HTTPException: 404 if the cart or line does not exist.
    """
    user_id = user.id if user is not None else None
    token = None if user is not None else _read_session_token(request)
    service = CartService(session)
    try:
        return await service.remove_item(user_id, token, product_id)
    except ItemNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


@router.post("/merge", response_model=CartOut)
async def merge_cart(
    request: Request,
    response: Response,
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CartOut:
    """Merge the guest cart (cookie) into the user cart after login (§7).

    A no-op when there is no guest cookie / cart. The guest cookie is cleared on
    success so the merged-away cart is not re-created.

    Args:
        request: Incoming request (guest cookie).
        response: Outgoing response (to clear the guest cookie).
        user: The authenticated user (required).
        session: Injected async DB session.

    Returns:
        CartOut: The re-rendered user cart.
    """
    service = CartService(session)
    token = _read_session_token(request)
    if token is None:
        return await service.get_cart(user.id, None)
    merged = await service.merge_guest_into_user(user.id, token)
    response.delete_cookie(SESSION_COOKIE)
    return merged
