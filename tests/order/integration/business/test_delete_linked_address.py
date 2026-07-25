"""BUG-006 / TASK-4: deleting an address linked by a courier order.

The ``order.delivery_address_id`` FK is ``ON DELETE SET NULL`` (the order keeps
its inline delivery snapshot, so the live Address is redundant after checkout).
Deleting a saved address that a courier order still references must therefore
succeed: the DB nulls ``order.delivery_address_id`` while the snapshot
(``delivery_name/city/street/zip``) is left untouched — no RESTRICT / HTTP 500.

Driven at the service layer (``AuthService.delete_address``) to assert the
DB-level ``SET NULL``: the order-domain HTTP client is guest-path only and does
not wire the authenticated address endpoint, so the service call is the direct,
un-awkward way to exercise the exact path the endpoint delegates to.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Address
from app.services.auth_service import AuthService
from tests.factories import AddressFactory, create_order, create_user, persist

pytestmark = pytest.mark.asyncio


async def test_delete_linked_address_nulls_order_fk_keeps_snapshot(
    db_session: AsyncSession,
) -> None:
    """Deleting a courier order's saved address nulls the FK, snapshot survives."""
    user = await create_user(db_session, email="linked@example.com")
    address = await persist(db_session, AddressFactory(user_id=user.id))
    order = await create_order(
        db_session,
        user=user,
        delivery_type="courier",
        delivery_address_id=address.id,
        delivery_name="Ion Popescu",
        delivery_city="Chisinau",
        delivery_street="Stefan cel Mare 1",
        delivery_zip="MD-2001",
    )
    address_id = address.id

    # Act: the exact path DELETE /users/me/addresses/{id} delegates to. redis is
    # unused by delete_address, so None keeps the test self-contained.
    await AuthService(session=db_session, redis=None).delete_address(
        user.id, address_id
    )

    # The address row is gone.
    assert await db_session.get(Address, address_id) is None
    # The FK was nulled by the DB (SET NULL), not left dangling.
    await db_session.refresh(order)
    assert order.delivery_address_id is None
    # The inline snapshot is untouched.
    assert order.delivery_name == "Ion Popescu"
    assert order.delivery_city == "Chisinau"
    assert order.delivery_street == "Stefan cel Mare 1"
    assert order.delivery_zip == "MD-2001"
