"""Order-history endpoints (§4 / §9).

Thin router over :class:`~app.repositories.order_repo.OrderRepository`:

* ``GET /orders`` — the authenticated user's orders (``current_user`` → 401
  without a token).
* ``GET /orders/{number}`` — a single order, authorized either by JWT (the
  caller owns it) OR, for a guest order, by matching ``?email=``. A wrong /
  missing email for a guest order yields 404 (never leak existence).

No business logic and no SQL live here. Mounted without the ``/api/v1`` prefix —
the integrator adds it.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user, guest_or_user
from app.core.db import get_session
from app.models.order import Order, OrderItem
from app.models.user import AppUser
from app.repositories.order_repo import OrderRepository
from app.schemas.order import OrderItemOut, OrderOut

router = APIRouter(prefix="/orders", tags=["orders"])


def _to_out(order: Order, items: list[OrderItem]) -> OrderOut:
    """Assemble an :class:`OrderOut` from an order and its lines.

    Args:
        order: The persisted order.
        items: The order's lines.

    Returns:
        OrderOut: The response projection.
    """
    return OrderOut(
        number=order.number,
        status=order.status,
        payment_status=order.payment_status,
        email=order.email,
        phone=order.phone,
        subtotal=order.subtotal,
        discount_total=order.discount_total,
        delivery_cost=order.delivery_cost,
        total=order.total,
        delivery_type=order.delivery_type,
        delivery_address_id=order.delivery_address_id,
        payment_method=order.payment_method,
        created_at=order.created_at,
        items=[OrderItemOut.model_validate(item) for item in items],
    )


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
    return [_to_out(order, items_by_order[order.id]) for order in orders]


@router.get("/{number}", response_model=OrderOut)
async def get_order(
    number: str,
    email: str | None = None,
    user: AppUser | None = Depends(guest_or_user),
    session: AsyncSession = Depends(get_session),
) -> OrderOut:
    """Return one order, authorized by JWT ownership OR guest ``?email=`` (§9).

    A logged-in caller may only see their own order. A guest (no/any token) must
    supply the matching ``email`` of a guest order. Any authorization miss —
    unknown number, not-your-order, wrong/absent email — returns 404 so order
    existence is never leaked.

    Args:
        number: The order number.
        email: Contact email (required for guest-order lookup).
        user: The authenticated user, or ``None`` for a guest.
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
    return _to_out(order, items)


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
