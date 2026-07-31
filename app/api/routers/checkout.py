"""Checkout endpoints (§9 / §4).

Thin router: it resolves the caller (authenticated user via ``guest_or_user``, or
a guest via the ``session_token`` cookie), delegates to
:class:`~app.services.checkout_service.CheckoutService`, and serializes the
result. Checkout and promo domain errors are :class:`DomainError` subclasses
rendered by the app's registered handler into the unified
``{error:{code,message,details?}}`` envelope, so the router does not catch them.
The one exception is :class:`MaibError` (a card-gateway failure), which is
translated here into a 502 ``payment_gateway_error`` envelope. No business logic
and no SQL live here.

``POST /checkout/quote`` is an idempotent predraft (no order created);
``POST /checkout`` creates the COD order atomically. Both accept guests by cookie.
Mounted without the ``/api/v1`` prefix — the integrator adds it.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import guest_or_user
from app.api.ratelimit import rate_limiter
from app.core.config import settings
from app.core.db import get_session
from app.core.errors import error_response
from app.core.redis import get_redis
from app.models.user import AppUser
from app.schemas.order import (
    CheckoutRequest,
    OrderOut,
    QuickBuyRequest,
    QuoteOut,
    QuoteRequest,
)
from app.services.checkout_service import CheckoutService
from app.services.payment.maib_client import MaibError

router = APIRouter(prefix="/checkout", tags=["checkout"])

# Guest cart cookie name (shared with the cart router, §2.3).
SESSION_COOKIE: str = "session_token"

# Accepted payment methods. ``card`` is only allowed when card payment is enabled.
_PAYMENT_METHODS: frozenset[str] = frozenset({"cod", "card"})


def _validate_payment_method(payment_method: str) -> None:
    """Reject an unknown method, or ``card`` while card payment is disabled.

    Args:
        payment_method: The requested method (``cod`` | ``card``).

    Raises:
        HTTPException: 422 for an unknown method; 400 ``card_payment_disabled``
            when ``card`` is requested but not enabled server-side.
    """
    if payment_method not in _PAYMENT_METHODS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_payment_method",
                "message": f"Unknown payment method: {payment_method}",
            },
        )
    if payment_method == "card" and not settings.card_payment_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "card_payment_disabled",
                "message": "Card payment is not available",
            },
        )


def _client_ip(request: Request) -> str | None:
    """Return the caller's IP for the maib fraud/3-DS signal, if resolvable.

    Prefers the first ``X-Forwarded-For`` hop (behind Cloudflare / a proxy),
    falling back to the socket peer. ``None`` when neither is available.

    Args:
        request: The incoming request.

    Returns:
        str | None: The client IP, or ``None``.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


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


def _caller_identity(
    request: Request,
    user: AppUser | None,
) -> tuple[int | None, UUID | None]:
    """Resolve ``(user_id, session_token)`` for the caller (user OR guest).

    Args:
        request: The incoming request (source of the guest cookie).
        user: The authenticated user, or ``None`` for a guest.

    Returns:
        tuple[int | None, UUID | None]: The user id (or ``None``) and the guest
            session token (or ``None`` for an authenticated caller).
    """
    if user is not None:
        return user.id, None
    return None, _read_session_token(request)


@router.post("/quote", response_model=QuoteOut)
async def quote(
    data: QuoteRequest,
    request: Request,
    user: AppUser | None = Depends(guest_or_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    lang: str = Query(default="ro", description="Display language (ru|ro)."),
) -> QuoteOut:
    """Return the checkout totals without creating an order (§9, idempotent).

    Args:
        data: Delivery choice for the predraft.
        request: Incoming request (guest cookie).
        user: The authenticated user, or ``None`` for a guest.
        session: Injected async DB session.
        lang: Requested line-name language (default ``ro``; mirrors checkout).

    Returns:
        QuoteOut: The subtotal / discount / delivery / total breakdown.

    Raises:
        EmptyCartError: 400 for an empty cart.
        DeliveryAddressRequiredError: 422 for courier without an address.
        DeliveryAddressForbiddenError: 403 for a ``delivery_address_id`` the
            caller does not own.
        PromoError: 400/404/409 for an unusable ``promo_code`` (rendered per its
            leaf status + code by the registered handler).
    """
    user_id, token = _caller_identity(request, user)
    service = CheckoutService(session, redis)
    return await service.quote(
        user_id=user_id,
        session_token=token,
        delivery_type=data.delivery_type,
        delivery_address_id=data.delivery_address_id,
        delivery_address=data.delivery_address,
        promo_code=data.promo_code,
        lang=lang,
        delivery_service=data.delivery_service,
        np_settlement_id=data.np_settlement_id,
        np_division_id=data.np_division_id,
        np_address=data.np_address,
        np_recipient_name=data.np_recipient_name,
    )


@router.post(
    "",
    response_model=OrderOut,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_409_CONFLICT: {"description": "Insufficient stock"}},
    dependencies=[Depends(rate_limiter(settings.rate_limit_checkout, "checkout"))],
)
async def checkout(
    data: CheckoutRequest,
    request: Request,
    user: AppUser | None = Depends(guest_or_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    lang: str = Query(default="ro", description="Snapshot language (ru|ro)."),
) -> OrderOut | JSONResponse:
    """Create a COD order from the caller's cart atomically (§9.6).

    Args:
        data: Contact + delivery details for the order.
        request: Incoming request (guest cookie).
        user: The authenticated user, or ``None`` for a guest order.
        session: Injected async DB session.
        lang: Language captured into each line's ``name_snapshot`` (default
            ``ro``; unsupported values fall back to the default).

    Returns:
        OrderOut | JSONResponse: The created order, or a 502
            ``payment_gateway_error`` envelope when the card gateway is
            unreachable after the order was persisted.

    Raises:
        EmptyCartError: 400 for an empty cart.
        DeliveryAddressRequiredError: 422 for courier without an address.
        DeliveryAddressForbiddenError: 403 for a ``delivery_address_id`` the
            caller does not own.
        OutOfStockError: 409 ``out_of_stock`` (with the offending ``product_id``
            in ``details``) when stock is insufficient at commit time.
        PromoError: 400/404/409 for an unusable ``promo_code``. All the above are
            rendered by the registered :class:`DomainError` handler.
    """
    _validate_payment_method(data.payment_method)
    user_id, token = _caller_identity(request, user)
    service = CheckoutService(session, redis)
    try:
        return await service.checkout(
            user_id=user_id,
            session_token=token,
            email=data.email,
            phone=data.phone,
            delivery_type=data.delivery_type,
            delivery_address_id=data.delivery_address_id,
            delivery_address=data.delivery_address,
            promo_code=data.promo_code,
            payment_method=data.payment_method,
            client_ip=_client_ip(request),
            lang=lang,
            delivery_service=data.delivery_service,
            np_settlement_id=data.np_settlement_id,
            np_division_id=data.np_division_id,
            np_address=data.np_address,
            np_recipient_name=data.np_recipient_name,
        )
    except MaibError:
        # The order was created (pending) but maib was unreachable; the payment
        # row stays pending for reconciliation. Surface a gateway error so the
        # storefront can prompt a retry.
        return error_response(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="payment_gateway_error",
            message="Payment provider is unavailable, please try again",
        )


@router.post(
    "/quick",
    response_model=OrderOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Product not found"},
        status.HTTP_409_CONFLICT: {"description": "Insufficient stock"},
    },
    dependencies=[Depends(rate_limiter(settings.rate_limit_checkout, "checkout"))],
)
async def quick_buy(
    data: QuickBuyRequest,
    session: AsyncSession = Depends(get_session),
    lang: str = Query(default="ro", description="Snapshot language (ru|ro)."),
) -> OrderOut:
    """Create a one-product COD order in one click (feature A2).

    A phone-only guest flow (no cart, no full checkout): the caller supplies a
    ``product_id`` and a ``phone`` and an operator calls back. Always free
    pickup. Reuses the same atomic path as ``POST /checkout`` and is rate-limited
    with the same ``checkout`` bucket to blunt spam.

    Args:
        data: Product + contact details for the one-click order.
        session: Injected async DB session.
        lang: Language captured into the line's ``name_snapshot`` (default
            ``ro``; unsupported values fall back to the default).

    Returns:
        OrderOut: The created order.

    Raises:
        ProductNotFoundError: 404 ``product_not_found`` for a missing/inactive
            product (with the ``product_id`` in ``details``).
        OutOfStockError: 409 ``out_of_stock`` when stock is insufficient (with
            the ``product_id`` in ``details``). Both are rendered by the
            registered :class:`DomainError` handler.
    """
    service = CheckoutService(session)
    return await service.quick_buy(
        product_id=data.product_id,
        variant_id=data.variant_id,
        phone=data.phone,
        name=data.name,
        email=data.email,
        qty=data.qty,
        lang=lang,
    )
