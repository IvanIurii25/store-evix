"""API tests for the customers back office (§6.2).

Covers: roster with batched order stats (count / lifetime spend excluding
canceled / last order); a customer with no orders (zero stats); email search;
the full detail (addresses + order history); 404 for an unknown id; and the
``current_staff`` guard.
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.models.user import Address, AppUser

pytestmark = pytest.mark.asyncio


async def _make_customer(
    session: AsyncSession,
    email: str,
    *,
    phone: str | None = None,
) -> AppUser:
    """Persist and return a non-staff customer."""
    user = AppUser(
        email=email,
        password_hash="x",
        phone=phone,
        is_active=True,
        is_staff=False,
    )
    session.add(user)
    await session.flush()
    return user


async def _make_order(
    session: AsyncSession,
    user_id: int,
    number: str,
    total: str,
    *,
    status: str = "done",
) -> Order:
    """Persist a minimal order for a customer."""
    order = Order(
        number=number,
        user_id=user_id,
        email="buyer@example.com",
        phone="+37360000000",
        status=status,
        subtotal=Decimal(total),
        total=Decimal(total),
        delivery_type="pickup",
    )
    session.add(order)
    await session.flush()
    return order


async def test_roster_with_stats(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Roster shows order count, lifetime spend (excl. canceled), last order."""
    buyer = await _make_customer(db_session, "buyer1@example.com")
    await _make_order(db_session, buyer.id, "ORD-1", "100.00", status="done")
    await _make_order(db_session, buyer.id, "ORD-2", "999.00", status="canceled")
    await _make_customer(db_session, "lurker@example.com")  # no orders

    resp = await client.get("/api/v1/admin/customers")
    assert resp.status_code == 200, resp.text
    by_email = {row["email"]: row for row in resp.json()["data"]}

    assert by_email["buyer1@example.com"]["orders_count"] == 2
    assert Decimal(by_email["buyer1@example.com"]["total_spent"]) == Decimal("100.00")
    assert by_email["buyer1@example.com"]["last_order_at"] is not None

    assert by_email["lurker@example.com"]["orders_count"] == 0
    assert Decimal(by_email["lurker@example.com"]["total_spent"]) == Decimal("0")
    assert by_email["lurker@example.com"]["last_order_at"] is None


async def test_search_by_email(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The ``q`` filter narrows the roster by email substring."""
    await _make_customer(db_session, "alice@example.com")
    await _make_customer(db_session, "bob@example.com")

    resp = await client.get("/api/v1/admin/customers", params={"q": "alice"})
    assert resp.status_code == 200
    emails = {row["email"] for row in resp.json()["data"]}
    assert emails == {"alice@example.com"}


async def test_customer_detail(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Detail returns profile, addresses, order history and stats."""
    buyer = await _make_customer(db_session, "detail@example.com", phone="+373601")
    db_session.add(
        Address(
            user_id=buyer.id,
            full_name="Detail Buyer",
            phone="+373601",
            city="Chișinău",
            street="str. Testului 1",
            is_default=True,
        )
    )
    await _make_order(db_session, buyer.id, "ORD-D1", "50.00")
    await _make_order(db_session, buyer.id, "ORD-D2", "70.00")
    await db_session.flush()

    resp = await client.get(f"/api/v1/admin/customers/{buyer.id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["email"] == "detail@example.com"
    assert body["orders_count"] == 2
    assert Decimal(body["total_spent"]) == Decimal("120.00")
    assert len(body["addresses"]) == 1
    assert body["addresses"][0]["is_default"] is True
    assert {o["number"] for o in body["orders"]} == {"ORD-D1", "ORD-D2"}


async def test_detail_unknown_is_404(client: AsyncClient) -> None:
    """An unknown customer id yields 404."""
    resp = await client.get("/api/v1/admin/customers/999999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_guard_blocks_anonymous(guest_client: AsyncClient) -> None:
    """Without a token the real ``current_staff`` dependency yields 401."""
    resp = await guest_client.get("/api/v1/admin/customers")
    assert resp.status_code == 401
