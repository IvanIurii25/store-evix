"""Admin orders API — list / detail / transition (stage B7, §10).

Thin back-office router, entirely behind ``Depends(current_staff)`` (JWT +
``is_staff``; a non-staff caller gets 403 from the dependency). It delegates to
:class:`~app.services.admin_order_service.AdminOrderService`, serializes results
into the shared :class:`~app.schemas.order.OrderOut`, and maps domain errors to
HTTP status codes (the app's HTTPException handler renders the unified
``{error:{code,message,details?}}`` envelope). No business logic and no SQL live
here.

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
from app.models.order import Order, OrderItem
from app.models.user import AppUser
from app.schemas.admin_orders import AdminOrderList, TransitionRequest
from app.schemas.order import OrderItemOut, OrderOut
from app.services.admin_order_service import AdminOrderService, OrderNotFoundError
from app.services.order_service import IllegalTransitionError
from app.services.payment.maib_client import MaibError
from app.services.payment.payment_service import (
    PaymentNotFoundError,
    PaymentService,
    RefundNotAllowedError,
)

router = APIRouter(
    prefix="/admin/orders",
    tags=["admin-orders"],
    dependencies=[Depends(current_staff)],
)

# Default page size for the admin order list (§4 envelope).
DEFAULT_PAGE_SIZE: int = 20


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
        delivery_name=order.delivery_name,
        delivery_city=order.delivery_city,
        delivery_street=order.delivery_street,
        delivery_zip=order.delivery_zip,
        payment_method=order.payment_method,
        created_at=order.created_at,
        items=[OrderItemOut.model_validate(item) for item in items],
    )


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
    page: int = Query(default=1, ge=1, description="1-based page number."),
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
    return AdminOrderList(
        data=[_to_out(order, items) for order, items in pairs],
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
    try:
        order, items = await service.get_order(number)
    except OrderNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "order_not_found", "message": str(exc)},
        ) from exc
    return _to_out(order, items)


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
    try:
        order, items = await service.transition(
            number,
            to_status=data.to_status,
            to_payment_status=data.to_payment_status,
            changed_by=f"admin:{staff.id}",
        )
    except OrderNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "order_not_found", "message": str(exc)},
        ) from exc
    except IllegalTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "illegal_transition", "message": str(exc)},
        ) from exc
    return _to_out(order, items)


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
    try:
        order, _ = await admin_service.get_order(number)
    except OrderNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "order_not_found", "message": str(exc)},
        ) from exc

    payment_service = PaymentService(session)
    try:
        await payment_service.refund(order)
    except RefundNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "refund_not_allowed", "message": str(exc)},
        ) from exc
    except PaymentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "payment_not_found", "message": str(exc)},
        ) from exc
    except MaibError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "payment_gateway_error",
                "message": "Refund failed at the payment provider",
            },
        ) from exc

    order, items = await admin_service.get_order(number)
    return _to_out(order, items)
