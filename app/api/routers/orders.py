"""Order-history endpoints (§4 / §9).

Thin router over :class:`~app.repositories.order_repo.OrderRepository`:

* ``GET /orders`` — the authenticated user's orders (``current_user`` → 401
  without a token).
* ``GET /orders/{number}`` — a single order the JWT caller owns (no PII in URL).
* ``POST /orders/{number}/lookup`` — guest order lookup by matching ``email`` in
  the request body (never a query string). A wrong / missing email yields 404
  (never leak existence).

No business logic and no SQL live here. Mounted without the ``/api/v1`` prefix —
the integrator adds it.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, guest_or_user
from app.core.db import get_session
from app.models.order import Order
from app.models.user import AppUser
from app.repositories.order_repo import OrderRepository
from app.schemas.order import OrderLookupIn, OrderOut

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
    repo = OrderRepository(session)
    orders = await repo.list_orders_for_user(user.id)
    items_by_order = await repo.list_items_for_orders([o.id for o in orders])
    return [
        OrderOut.from_order(order, items_by_order[order.id]) for order in orders
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
        HTTPException: 404 if the order is absent or not owned by the caller.
    """
    return await _fetch_authorized(number, user, None, session)


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
        HTTPException: 404 if the order is absent or the email does not match.
    """
    return await _fetch_authorized(number, user, payload.email, session)


async def _fetch_authorized(
    number: str,
    user: AppUser | None,
    email: str | None,
    session: AsyncSession,
) -> OrderOut:
    """Load an order the caller may view (JWT owner or matching email), or 404.

    Args:
        number: The order number.
        user: The authenticated user, or ``None``.
        email: The email to match for guest lookup, or ``None`` for owner-only.
        session: Injected async DB session.

    Returns:
        OrderOut: The order with its lines.

    Raises:
        HTTPException: 404 if the order is absent or not authorized to the caller.
    """
    repo = OrderRepository(session)
    order = await repo.get_order_by_number(number)
    if order is None or not _authorized(order, user, email):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Order not found"
        )
    items = (await repo.list_items_for_orders([order.id]))[order.id]
    return OrderOut.from_order(order, items)


def _authorized(order: Order, user: AppUser | None, email: str | None) -> bool:
    """Return whether the caller may view ``order`` (JWT owner or guest email).

    Args:
        order: The order being accessed.
        user: The authenticated user, or ``None``.
        email: The email supplied for guest lookup.

    Returns:
        bool: ``True`` if the caller owns the order (by user id) or supplied the
            matching guest-order email.
    """
    if user is not None and order.user_id == user.id:
        return True
    if email is not None and order.email.lower() == email.lower():
        return True
    return False
