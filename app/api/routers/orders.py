"""Order-history endpoints (§4 / §9).

Thin router over :class:`~app.services.order_service.OrderService` (read side):

* ``GET /orders`` — the authenticated user's orders (``current_user`` → 401
  without a token).
* ``GET /orders/{number}`` — a single order the JWT caller owns (no PII in URL).
* ``POST /orders/{number}/lookup`` — guest order lookup by matching ``email`` in
  the request body (never a query string). A wrong / missing email yields 404
  (never leak existence).

No business logic and no SQL live here. Mounted without the ``/api/v1`` prefix —
the integrator adds it.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, guest_or_user
from app.core.db import get_session
from app.models.user import AppUser
from app.schemas.order import OrderLookupIn, OrderOut
from app.services.order_service import OrderService

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("", response_model=list[OrderOut])
async def list_orders(
    user: AppUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[OrderOut]:
    """Return the authenticated user's orders, newest first (§4).

    Args:
        user: The authenticated user (required — 401 without a token).
        session: Injected async DB session.

    Returns:
        list[OrderOut]: The user's orders with their lines.
    """
    service = OrderService(session)
    rows = await service.list_for_user(user.id)
    return [
        OrderOut.from_order(order, items, carrier) for order, items, carrier in rows
    ]


@router.get("/{number}", response_model=OrderOut)
async def get_order(
    number: str,
    user: AppUser | None = Depends(guest_or_user),
    session: AsyncSession = Depends(get_session),
) -> OrderOut:
    """Return one order the JWT caller owns (§9). No personal data in the URL.

    A logged-in caller may only see their own order; anyone else (a guest, or a
    different user) gets 404 so existence is never leaked. Guests look up their
    order via ``POST /orders/{number}/lookup`` (email in the body).

    Args:
        number: The order number.
        user: The authenticated user, or ``None`` for a guest.
        session: Injected async DB session.

    Returns:
        OrderOut: The order with its lines.

    Raises:
        OrderNotFoundError: 404 if the order is absent or not owned by the
            caller (rendered by the unified ``DomainError`` handler).
    """
    service = OrderService(session)
    order, items, carrier = await service.get_for_user(number, user, None)
    return OrderOut.from_order(order, items, carrier)


@router.post("/{number}/lookup", response_model=OrderOut)
async def lookup_order(
    number: str,
    payload: OrderLookupIn,
    user: AppUser | None = Depends(guest_or_user),
    session: AsyncSession = Depends(get_session),
) -> OrderOut:
    """Look up an order by number + matching email in the body (§9).

    The email travels in the request body, never a query string, so it is not
    captured in access logs or browser history (LP195/2024 — no PII in URLs). Any
    authorization miss (unknown number, wrong/absent email, not the owner)
    returns 404.

    Args:
        number: The order number.
        payload: The lookup body carrying the contact ``email``.
        user: The authenticated user, or ``None`` for a guest.
        session: Injected async DB session.

    Returns:
        OrderOut: The order with its lines.

    Raises:
        OrderNotFoundError: 404 if the order is absent or the email does not
            match (rendered by the unified ``DomainError`` handler).
    """
    service = OrderService(session)
    order, items, carrier = await service.get_for_user(number, user, payload.email)
    # Guest lookup: number + email is a weak credential, so the waybill number
    # (a tracking key for the parcel) is withheld — see NovaPostOrderOut.
    return OrderOut.from_order(order, items, carrier, hide_awb=user is None)
