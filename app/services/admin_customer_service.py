"""Customers back-office service: roster + detail with order stats (§6.2).

Thin business layer over :class:`~app.repositories.admin_customer_repo`. It
resolves each customer's order stats (count / lifetime spend / last order) via a
single batched query for a whole page — never a per-customer N+1 — and assembles
the detail view (profile + addresses + order history + stats).

Returns raw domain objects (models, the :class:`CustomerStats` dataclass, and the
:class:`CustomerDetailData` bundle); the router maps them to response schemas.
Raises :class:`CustomerNotFoundError` (mapped to 404) for an unknown id.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.models.user import Address, AppUser
from app.repositories.admin_customer_repo import (
    AdminCustomerRepository,
    CustomerStats,
)

# Stats for a customer with no orders (kept out of the batched query result).
_ZERO_STATS = CustomerStats(
    orders_count=0, total_spent=Decimal("0"), last_order_at=None
)


class CustomerNotFoundError(Exception):
    """No customer exists for the requested id (mapped to 404 by the router)."""

    code = "not_found"


@dataclass(frozen=True)
class CustomerDetailData:
    """Everything the detail view needs, assembled once (§6.2)."""

    user: AppUser
    stats: CustomerStats
    addresses: list[Address]
    orders: list[Order]


class AdminCustomerService:
    """Back-office customer operations bound to one session (§6.2)."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and its repository.

        Args:
            session: Active async session (request- or test-scoped).
        """
        self.session = session
        self.repo = AdminCustomerRepository(session)

    async def list_customers(
        self,
        *,
        query: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[tuple[AppUser, CustomerStats]], int]:
        """Return one page of customers paired with their order stats + total.

        Stats for the whole page are resolved in a single batched query (no
        per-customer N+1); customers with no orders get a zero-stats default.

        Args:
            query: Free-text search over email / phone, or ``None``.
            page: 1-based page number.
            page_size: Page size.

        Returns:
            tuple: ``(pairs, total)`` where ``pairs`` is ``[(user, stats), ...]``.
        """
        offset = (page - 1) * page_size
        users = await self.repo.list_customers(
            query=query, offset=offset, limit=page_size
        )
        total = await self.repo.count_customers(query=query)
        stats = await self.repo.stats_for([user.id for user in users])
        pairs = [(user, stats.get(user.id, _ZERO_STATS)) for user in users]
        return pairs, total

    async def get_customer(self, user_id: int) -> CustomerDetailData:
        """Return the full detail bundle for one customer, or raise 404.

        Args:
            user_id: The customer id.

        Returns:
            CustomerDetailData: Profile + stats + addresses + order history.

        Raises:
            CustomerNotFoundError: If no customer has that id.
        """
        user = await self.repo.get_customer(user_id)
        if user is None:
            raise CustomerNotFoundError(f"Customer not found: {user_id}")
        stats = (await self.repo.stats_for([user_id])).get(user_id, _ZERO_STATS)
        addresses = await self.repo.list_addresses(user_id)
        orders = await self.repo.list_orders(user_id)
        return CustomerDetailData(
            user=user,
            stats=stats,
            addresses=addresses,
            orders=orders,
        )
