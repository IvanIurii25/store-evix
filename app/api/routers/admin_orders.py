"""Admin orders API — list / detail / transition (stage B7, §10).

Thin back-office router, entirely behind ``Depends(current_staff)`` (JWT +
``is_staff``; a non-staff caller gets 403 from the dependency). It delegates to
:class:`~app.services.admin_order_service.AdminOrderService` and serializes results
into the shared :class:`~app.schemas.order.OrderOut`. Domain errors raised by the
services subclass :class:`~app.core.errors.DomainError` and are rendered into the
unified ``{error:{code,message,details?}}`` envelope by the registered handler; only
the maib gateway failure (not a ``DomainError``) is mapped here, to 502. No business
logic and no SQL live here.

Endpoints (paths carry ``/admin``; the ``/api/v1`` prefix is added by the
integrator when mounting):

* ``GET  /admin/orders?status=&q=&page=`` — filtered, paginated order list.
* ``GET  /admin/orders/{number}`` — one order with its lines.
* ``POST /admin/orders/{number}/transition`` — status/payment move via the shared
  state machine (§8), the same one the storefront uses.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.core.db import get_session
from app.models.user import AppUser
from app.schemas.admin_orders import AdminOrderList, TransitionRequest
from app.schemas.order import OrderOut
from app.services.admin_order_service import AdminOrderService
from app.services.payment.maib_client import MaibError
from app.services.payment.payment_service import PaymentService

router = APIRouter(
    prefix="/admin/orders",
    tags=["admin-orders"],
    dependencies=[Depends(current_staff)],
)

# Default page size for the admin order list (§4 envelope).
DEFAULT_PAGE_SIZE: int = 20


@router.get("", response_model=AdminOrderList)
async def list_orders(
    status: str | None = Query(
        default=None,
        description="Exact fulfilment status filter.",
    ),
    q: str | None = Query(
        default=None,
        description="Search over number / email / phone.",
    ),
    page: int = Query(default=1, ge=1, le=10000, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=100,
        description="Items per page.",
    ),
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> AdminOrderList:
    """Return a filtered, paginated page of orders for the back office (§10).

    Args:
        status: Optional exact fulfilment-status filter.
        q: Optional free-text search over number / email / phone.
        page: 1-based page number.
        page_size: Items per page.
        _staff: The authenticated staff user (guards the endpoint).
        session: Injected async DB session.

    Returns:
        AdminOrderList: The ``{data, total, page, page_size}`` envelope.
    """
    service = AdminOrderService(session)
    pairs, total = await service.list_orders(
        status=status,
        query=q,
        page=page,
        page_size=page_size,
    )
    carriers = await service.novapost_map([order.id for order, _items in pairs])
    return AdminOrderList(
        data=[
            OrderOut.from_order(order, items, carriers.get(order.id))
            for order, items in pairs
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{number}", response_model=OrderOut)
async def get_order(
    number: str,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> OrderOut:
    """Return one order with its lines by number (§10).

    Args:
        number: The order number.
        _staff: The authenticated staff user (guards the endpoint).
        session: Injected async DB session.

    Returns:
        OrderOut: The order with its lines.

    Raises:
        HTTPException: 404 if no order has that number.
    """
    service = AdminOrderService(session)
    order, items = await service.get_order(number)
    return OrderOut.from_order(order, items, await service.novapost(order.id))


@router.post("/{number}/transition", response_model=OrderOut)
async def transition_order(
    number: str,
    data: TransitionRequest,
    staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> OrderOut:
    """Apply a status/payment transition to an order via the shared machine (§8).

    Reuses the storefront ``OrderService.transition`` — the same state machine and
    the same history-writing path — so admin and storefront never diverge. An
    illegal move is rejected with a 409 domain error.

    Args:
        number: The order number to transition.
        data: The requested status and/or payment move.
        staff: The authenticated staff user (recorded as the actor).
        session: Injected async DB session.

    Returns:
        OrderOut: The updated order with its lines.

    Raises:
        HTTPException: 404 if the order is unknown; 409 for an illegal move.
    """
    service = AdminOrderService(session)
    order, items = await service.transition(
        number,
        to_status=data.to_status,
        to_payment_status=data.to_payment_status,
        changed_by=f"admin:{staff.id}",
    )
    return OrderOut.from_order(order, items, await service.novapost(order.id))


@router.post("/{number}/refund", response_model=OrderOut)
async def refund_order(
    number: str,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> OrderOut:
    """Refund a paid card order via maib and mark it ``refunded`` (§card-payment).

    Only a card order whose ``payment_status`` is ``paid`` is refundable; anything
    else is rejected 409. Delegates the maib refund + the ``refunded`` transition
    to :class:`~app.services.payment.payment_service.PaymentService`.

    Args:
        number: The order number to refund.
        staff: The authenticated staff user (guards the endpoint).
        session: Injected async DB session.

    Returns:
        OrderOut: The refunded order with its lines.

    Raises:
        HTTPException: 404 if the order/payment is unknown; 409 if the order is
            not a paid card order; 502 if the maib refund call fails.
    """
    admin_service = AdminOrderService(session)
    # Lock the order for the whole refund (guard + maib call + transition) so a
    # concurrent refund of the same order blocks, then re-reads the now-refunded
    # status and is rejected before it can call maib again. An unknown order
    # surfaces as an ``OrderNotFoundError`` (404) domain error.
    order, _ = await admin_service.get_order(number, for_update=True)

    payment_service = PaymentService(session)
    # ``RefundNotAllowedError`` (409) / ``PaymentNotFoundError`` (404) are domain
    # errors rendered by the unified handler; only the gateway failure is mapped
    # here (maib is not a ``DomainError``).
    try:
        await payment_service.refund(order)
    except MaibError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "payment_gateway_error",
                "message": "Refund failed at the payment provider",
            },
        ) from exc

    order, items = await admin_service.get_order(number)
    return OrderOut.from_order(order, items, await admin_service.novapost(order.id))
