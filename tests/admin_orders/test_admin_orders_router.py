"""Router-line tests for the admin orders API (B7, §10).

Fills the router branches the existing ``test_admin_orders_api.py`` does not
reach — chiefly the **transition** endpoint's not-found path (an unknown number
handed to ``POST /admin/orders/{number}/transition`` → 404), plus the list and
detail happy paths asserted at the HTTP envelope level so the ``AdminOrderList``
assembly and ``_to_out`` projection are exercised end to end.

Complements (does not duplicate) the assertions already in
``test_admin_orders_api.py``.

Run with ``EVIX_TEST_DB=evix_test_admin_orders``.
"""

import pytest

_ORDERS_URL = "/api/v1/admin/orders"

pytestmark = pytest.mark.asyncio

# Named constants for the asserted HTTP contract.
_OK: int = 200
_NOT_FOUND: int = 404
_CONFLICT: int = 409
_UNKNOWN_NUMBER: str = "NO-SUCH-ORDER"


class TestAdminOrdersRouterTransition:
    """``POST /admin/orders/{number}/transition`` — 404 / 409 error mapping."""

    async def test_transition_unknown_number_maps_to_404(self, client):
        """An unknown number surfaces the not-found domain envelope (404)."""
        # Arrange: no matching order exists.

        # Act: attempt a legal-looking move on a missing order.
        resp = await client.post(
            f"{_ORDERS_URL}/{_UNKNOWN_NUMBER}/transition",
            json={"to_status": "confirmed"},
        )

        # Assert: the router maps OrderNotFoundError to a 404 envelope.
        assert resp.status_code == _NOT_FOUND, "missing order → 404"
        assert resp.json()["error"]["code"] == "order_not_found", (
            "the not-found domain code is rendered"
        )

    async def test_transition_illegal_payment_move_maps_to_409(
        self, client, make_order
    ):
        """An illegal payment move surfaces the conflict envelope (409)."""
        # Arrange: a paid order cannot go back to pending (payment axis).
        await make_order("R-PAY-BAD", payment_status="paid")

        # Act: request the disallowed payment reversal.
        resp = await client.post(
            f"{_ORDERS_URL}/R-PAY-BAD/transition",
            json={"to_payment_status": "pending"},
        )

        # Assert: the router maps IllegalTransitionError to a 409 envelope.
        assert resp.status_code == _CONFLICT, "illegal payment move → 409"
        assert resp.json()["error"]["code"] == "illegal_transition", (
            "the illegal-transition domain code is rendered"
        )


class TestAdminOrdersRouterRead:
    """``GET`` list + detail — envelope assembly and line projection."""

    async def test_list_envelope_carries_page_metadata(self, client, make_order):
        """The list envelope echoes the requested page and page_size."""
        # Arrange: a single order so the page is non-empty.
        await make_order("R-LIST")

        # Act: request a specific, non-default page size.
        resp = await client.get(_ORDERS_URL, params={"page": 1, "page_size": 5})

        # Assert: the AdminOrderList envelope carries the paging metadata.
        assert resp.status_code == _OK, "list read succeeds"
        body = resp.json()
        assert body["page"] == 1, "requested page is echoed"
        assert body["page_size"] == 5, "requested page_size is echoed"

    async def test_detail_projects_order_lines(self, client, make_order):
        """Detail projects the order and its snapshotted line via _to_out."""
        # Arrange: one order with its single seeded line.
        await make_order("R-DET")

        # Act: fetch the detail by number.
        resp = await client.get(f"{_ORDERS_URL}/R-DET")

        # Assert: the projection includes the order number and its line.
        assert resp.status_code == _OK, "detail read succeeds"
        body = resp.json()
        assert body["number"] == "R-DET", "the requested order is projected"
        assert len(body["items"]) == 1, "the order's line is projected"
