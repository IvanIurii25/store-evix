"""Admin customers API — roster + detail with order stats (§6.2).

Thin back-office router behind ``Depends(current_staff)`` (JWT + ``is_staff`` →
403 for non-staff). It delegates to
:class:`~app.services.admin_customer_service.AdminCustomerService`, assembles the
response schemas. The service raises :class:`CustomerNotFoundError` (a
``DomainError`` rendered as 404 ``not_found`` by the registered handler); no
error mapping, business logic, or SQL lives here.

Endpoints (the ``/api/v1`` prefix is added by the integrator when mounting):

* ``GET /admin/customers?q=&page=&page_size=`` — paginated customer roster with
  per-customer order stats (count / lifetime spend / last order).
* ``GET /admin/customers/{user_id}`` — full profile: contact + addresses + order
  history + stats.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.core.db import get_session
from app.models.user import AppUser
from app.repositories.admin_customer_repo import CustomerStats
from app.schemas.admin_customers import (
    CustomerAddress,
    CustomerDetail,
    CustomerList,
    CustomerListItem,
    CustomerOrderSummary,
)
from app.services.admin_customer_service import (
    AdminCustomerService,
    CustomerDetailData,
)

router = APIRouter(
    prefix="/admin/customers",
    tags=["admin-customers"],
    dependencies=[Depends(current_staff)],
)

# Default page size for the customer roster (§4 envelope).
DEFAULT_PAGE_SIZE: int = 20


def _to_list_item(user: AppUser, stats: CustomerStats) -> CustomerListItem:
    """Project a customer + stats into a roster row."""
    return CustomerListItem(
        id=user.id,
        email=user.email,
        phone=user.phone,
        created_at=user.created_at,
        orders_count=stats.orders_count,
        total_spent=stats.total_spent,
        last_order_at=stats.last_order_at,
    )


def _to_detail(data: CustomerDetailData) -> CustomerDetail:
    """Assemble the full customer detail response from the service bundle."""
    return CustomerDetail(
        id=data.user.id,
        email=data.user.email,
        phone=data.user.phone,
        is_active=data.user.is_active,
        created_at=data.user.created_at,
        loyalty_points=data.user.loyalty_points,
        orders_count=data.stats.orders_count,
        total_spent=data.stats.total_spent,
        last_order_at=data.stats.last_order_at,
        addresses=[CustomerAddress.model_validate(a) for a in data.addresses],
        orders=[CustomerOrderSummary.model_validate(o) for o in data.orders],
    )


@router.get("", response_model=CustomerList)
async def list_customers(
    q: str | None = Query(default=None, description="Search over email / phone."),
    page: int = Query(default=1, ge=1, le=10000, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=100,
        description="Items per page.",
    ),
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> CustomerList:
    """Return a paginated customer roster with per-customer order stats (§6.2)."""
    service = AdminCustomerService(session)
    pairs, total = await service.list_customers(query=q, page=page, page_size=page_size)
    return CustomerList(
        data=[_to_list_item(user, stats) for user, stats in pairs],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{user_id}", response_model=CustomerDetail)
async def get_customer(
    user_id: int,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> CustomerDetail:
    """Return one customer's full profile, addresses, orders and stats (§6.2).

    An unknown ``user_id`` makes the service raise
    :class:`~app.services.admin_customer_service.CustomerNotFoundError`, which the
    registered domain-error handler renders as 404 ``not_found``.
    """
    service = AdminCustomerService(session)
    data = await service.get_customer(user_id)
    return _to_detail(data)
