"""Checkout endpoints (§9 / §4).

Thin router: it resolves the caller (authenticated user via ``guest_or_user``, or
a guest via the ``session_token`` cookie), delegates to
:class:`~app.services.checkout_service.CheckoutService`, serializes the result,
and maps domain errors to HTTP status codes (the app's HTTPException handler
renders the unified ``{error:{code,message,details?}}`` envelope). No business
logic and no SQL live here.

``POST /checkout/quote`` is an idempotent predraft (no order created);
``POST /checkout`` creates the COD order atomically. Both accept guests by cookie.
Mounted without the ``/api/v1`` prefix — the integrator adds it.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import guest_or_user
from app.api.ratelimit import rate_limiter
from app.core.config import settings
from app.core.db import get_session
from app.core.errors import error_response
from app.models.user import AppUser
from app.schemas.order import (
    CheckoutRequest,
    OrderOut,
    QuickBuyRequest,
    QuoteOut,
    QuoteRequest,
)
from app.services.checkout_service import (
    CheckoutService,
    DeliveryAddressForbiddenError,
    DeliveryAddressRequiredError,
    EmptyCartError,
    OutOfStockError,
    ProductNotFoundError,
)
from app.services.payment.maib_client import MaibError
from app.services.promo_service import (
    PromoExpiredError,
    PromoInvalidError,
    PromoMinOrderError,
    PromoUsageLimitError,
)

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


def _promo_http_error(
    exc: PromoInvalidError
    | PromoExpiredError
    | PromoMinOrderError
    | PromoUsageLimitError,
) -> HTTPException:
    """Map a promo domain error to its HTTP status + coded envelope detail.

    ``promo_invalid`` (unknown/disabled) → 404; ``promo_expired`` /
    ``promo_min_order`` → 400; ``promo_usage_limit`` → 409. The ``code`` is
    surfaced verbatim so the storefront can show a targeted message.

    Args:
        exc: The raised promo error.

    Returns:
        HTTPException: The mapped exception the router re-raises.
    """
    if isinstance(exc, PromoInvalidError):
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, PromoUsageLimitError):
        http_status = status.HTTP_409_CONFLICT
    else:
        http_status = status.HTTP_400_BAD_REQUEST
    return HTTPException(
        status_code=http_status,
        detail={"code": exc.code, "message": str(exc)},
    )


@router.post("/quote", response_model=QuoteOut)
async def quote(
    data: QuoteRequest,
    request: Request,
    user: AppUser | None = Depends(guest_or_user),
    session: AsyncSession = Depends(get_session),
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
        HTTPException: 400 for an empty cart; 422 for courier without an address;
            403 for a ``delivery_address_id`` the caller does not own.
    """
    user_id, token = _caller_identity(request, user)
    service = CheckoutService(session)
    try:
        return await service.quote(
            user_id=user_id,
            session_token=token,
            delivery_type=data.delivery_type,
            delivery_address_id=data.delivery_address_id,
            delivery_address=data.delivery_address,
            promo_code=data.promo_code,
            lang=lang,
        )
    except EmptyCartError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except DeliveryAddressForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except DeliveryAddressRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except (
        PromoInvalidError,
        PromoExpiredError,
        PromoMinOrderError,
        PromoUsageLimitError,
    ) as exc:
        raise _promo_http_error(exc) from exc


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
        OrderOut | JSONResponse: The created order, or a 409 ``out_of_stock``
            envelope when stock is insufficient.

    Raises:
        HTTPException: 400 empty cart; 422 courier without an address; 403 for a
            ``delivery_address_id`` the caller does not own.
    """
    _validate_payment_method(data.payment_method)
    user_id, token = _caller_identity(request, user)
    service = CheckoutService(session)
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
        )
    except EmptyCartError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except DeliveryAddressForbiddenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except DeliveryAddressRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except (
        PromoInvalidError,
        PromoExpiredError,
        PromoMinOrderError,
        PromoUsageLimitError,
    ) as exc:
        raise _promo_http_error(exc) from exc
    except OutOfStockError as exc:
        # Return the envelope directly so ``error.code`` is exactly
        # ``out_of_stock`` (the shared HTTPException handler always emits
        # ``http_error``, which the contract forbids here).
        return error_response(
            status_code=status.HTTP_409_CONFLICT,
            code="out_of_stock",
            message=str(exc),
            details={"product_id": exc.product_id},
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
) -> OrderOut | JSONResponse:
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
        OrderOut | JSONResponse: The created order, a 404 ``product_not_found``
            envelope for a missing/inactive product, or a 409 ``out_of_stock``
            envelope when stock is insufficient.
    """
    service = CheckoutService(session)
    try:
        return await service.quick_buy(
            product_id=data.product_id,
            phone=data.phone,
            name=data.name,
            email=data.email,
            qty=data.qty,
            lang=lang,
        )
    except ProductNotFoundError as exc:
        return error_response(
            status_code=status.HTTP_404_NOT_FOUND,
            code="product_not_found",
            message=str(exc),
            details={"product_id": exc.product_id},
        )
    except OutOfStockError as exc:
        return error_response(
            status_code=status.HTTP_409_CONFLICT,
            code="out_of_stock",
            message=str(exc),
            details={"product_id": exc.product_id},
        )
