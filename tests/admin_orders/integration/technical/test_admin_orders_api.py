"""Adversarial HTTP tests for the admin orders API (B7, §10).

Covers the DoD surface:

* list all + ``status`` filter + ``q`` search (by number / email);
* order detail by number (+ 404 for an unknown number);
* transition ``pending → paid`` (payment axis) and ``new → confirmed`` (status
  axis) → 200 and an ``order_status_history`` row written;
* an illegal transition rejected with the unified domain envelope;
* the ``current_staff`` guard: no token → 401.
"""

from decimal import Decimal

import pytest

from app.repositories.order_repo import OrderRepository

_ORDERS_URL = "/api/v1/admin/orders"

pytestmark = pytest.mark.asyncio


async def test_list_all_orders(client, make_order):
    """The list returns every order in the ``{data, total, page, page_size}`` shape."""
    await make_order("O-1")
    await make_order("O-2")

    resp = await client.get(_ORDERS_URL)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert {row["number"] for row in body["data"]} == {"O-1", "O-2"}


async def test_filter_by_status(client, make_order):
    """``?status=`` returns only orders in that fulfilment status."""
    await make_order("O-NEW", status="new")
    await make_order("O-CONF", status="confirmed")

    resp = await client.get(_ORDERS_URL, params={"status": "confirmed"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["data"][0]["number"] == "O-CONF"


async def test_search_by_number(client, make_order):
    """``?q=`` matches on the order number (partial, case-insensitive)."""
    await make_order("ORD-ABC-1")
    await make_order("ORD-XYZ-2")

    resp = await client.get(_ORDERS_URL, params={"q": "abc"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["data"][0]["number"] == "ORD-ABC-1"


async def test_search_by_email(client, make_order):
    """``?q=`` matches on the contact email."""
    await make_order("O-A", email="alice@shop.md")
    await make_order("O-B", email="bob@shop.md")

    resp = await client.get(_ORDERS_URL, params={"q": "alice"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["data"][0]["number"] == "O-A"


async def test_order_detail_by_number(client, make_order):
    """Detail returns the order with its snapshotted lines."""
    await make_order("O-DET", total=Decimal("250.00"))

    resp = await client.get(f"{_ORDERS_URL}/O-DET")

    assert resp.status_code == 200
    body = resp.json()
    assert body["number"] == "O-DET"
    assert body["total"] == "250.00"
    assert len(body["items"]) == 1
    assert body["items"][0]["name_snapshot"] == "Sample product"


async def test_detail_unknown_number_404(client):
    """An unknown number yields a 404 domain envelope."""
    resp = await client.get(f"{_ORDERS_URL}/NOPE")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "order_not_found"


async def test_transition_payment_pending_to_paid(client, make_order, db_session):
    """``pending → paid`` succeeds and writes a status-history row."""
    order = await make_order("O-PAY", payment_status="pending")

    resp = await client.post(
        f"{_ORDERS_URL}/O-PAY/transition",
        json={"to_payment_status": "paid"},
    )

    assert resp.status_code == 200
    assert resp.json()["payment_status"] == "paid"
    history = await OrderRepository(db_session).count_status_history(order.id)
    assert history == 1


async def test_transition_status_new_to_confirmed(client, make_order, db_session):
    """``new → confirmed`` succeeds and writes a status-history row."""
    order = await make_order("O-CONFIRM", status="new")

    resp = await client.post(
        f"{_ORDERS_URL}/O-CONFIRM/transition",
        json={"to_status": "confirmed"},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"
    history = await OrderRepository(db_session).count_status_history(order.id)
    assert history == 1


async def test_illegal_transition_rejected(client, make_order, db_session):
    """An illegal move (``new → done``) is rejected as a domain error, no history."""
    order = await make_order("O-BAD", status="new")

    resp = await client.post(
        f"{_ORDERS_URL}/O-BAD/transition",
        json={"to_status": "done"},
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "illegal_transition"
    history = await OrderRepository(db_session).count_status_history(order.id)
    assert history == 0


async def test_transition_empty_body_422(client, make_order):
    """A body with neither axis is rejected at validation (422)."""
    await make_order("O-EMPTY")

    resp = await client.post(f"{_ORDERS_URL}/O-EMPTY/transition", json={})

    assert resp.status_code == 422


async def test_guard_requires_staff(guest_client, make_order):
    """Without a token the real ``current_staff`` dependency yields 401."""
    await make_order("O-GUARD")

    resp = await guest_client.get(_ORDERS_URL)

    assert resp.status_code == 401
