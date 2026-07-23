"""Direct service tests for :class:`AdminOrderService` (B7, §10).

Drives the service layer without the router so every business branch is
exercised in isolation:

* ``list_orders`` — pairing orders with their lines, ``status`` filter and
  offset pagination (the ``(page - 1) * page_size`` math);
* ``get_order`` — the happy path (order + hydrated lines) and the 404 domain
  error via the private ``_require_order`` guard;
* ``transition`` — a legal move (status + payment axes) that writes history, an
  illegal move that raises before mutation, and the not-found guard.

These complement (do not duplicate) the HTTP-level assertions in
``test_admin_orders_api.py`` — here we assert on the returned domain objects and
the ``order_status_history`` audit trail directly.

Run with ``EVIX_TEST_DB=evix_test_admin_orders``.
"""

from decimal import Decimal

import pytest

from app.repositories.order_repo import OrderRepository
from app.services.admin_order_service import AdminOrderService, OrderNotFoundError
from app.services.order_service import IllegalTransitionError

pytestmark = pytest.mark.asyncio

# Named constants (no magic literals in assertions).
_PAGE_SIZE: int = 2
_FIRST_PAGE: int = 1
_SECOND_PAGE: int = 2
_EXPECTED_SEEDED: int = 3
_ONE_HISTORY_ROW: int = 1
_NO_HISTORY_ROWS: int = 0
_UNKNOWN_NUMBER: str = "DOES-NOT-EXIST"


class TestAdminOrderServiceList:
    """``AdminOrderService.list_orders`` — pairing, filtering, pagination."""

    async def test_list_pairs_each_order_with_its_lines(self, db_session, make_order):
        """Every returned pair carries the order and its own hydrated lines."""
        # Arrange: two orders, each seeded with exactly one line by make_order.
        await make_order("SVC-L1")
        await make_order("SVC-L2")
        service = AdminOrderService(db_session)

        # Act: request a page large enough to hold both orders.
        pairs, total = await service.list_orders(
            status=None, query=None, page=_FIRST_PAGE, page_size=10
        )

        # Assert: total reflects both, and each pair has its single line.
        assert total == 2, "both seeded orders must be counted"
        assert {order.number for order, _ in pairs} == {"SVC-L1", "SVC-L2"}, (
            "both orders must appear in the page"
        )
        assert all(len(items) == 1 for _, items in pairs), (
            "each order must be paired with its one hydrated line"
        )

    async def test_list_filtered_by_status_returns_only_matching(
        self, db_session, make_order
    ):
        """The ``status`` filter narrows the page and the total to that status."""
        # Arrange: one order per fulfilment status.
        await make_order("SVC-NEW", status="new")
        await make_order("SVC-CONF", status="confirmed")
        service = AdminOrderService(db_session)

        # Act: filter on the confirmed status only.
        pairs, total = await service.list_orders(
            status="confirmed", query=None, page=_FIRST_PAGE, page_size=10
        )

        # Assert: exactly the confirmed order is returned and counted.
        assert total == 1, "only the confirmed order matches the filter"
        assert pairs[0][0].number == "SVC-CONF", "the confirmed order is returned"

    async def test_list_second_page_applies_offset(self, db_session, make_order):
        """Page 2 with size 2 skips the first slice while total stays whole."""
        # Arrange: three orders so page 2 (size 2) holds exactly the leftover one.
        for idx in range(_EXPECTED_SEEDED):
            await make_order(f"SVC-P{idx}")
        service = AdminOrderService(db_session)

        # Act: fetch the second page.
        pairs, total = await service.list_orders(
            status=None, query=None, page=_SECOND_PAGE, page_size=_PAGE_SIZE
        )

        # Assert: total counts all rows; the offset leaves one row on page 2.
        assert total == _EXPECTED_SEEDED, "total spans every page, not just this one"
        assert len(pairs) == 1, "offset (page-1)*size leaves one order on page 2"


class TestAdminOrderServiceGet:
    """``AdminOrderService.get_order`` — hydration and the not-found guard."""

    async def test_get_known_order_returns_order_with_lines(
        self, db_session, make_order
    ):
        """A known number yields the order paired with its hydrated lines."""
        # Arrange: one order with a known total and its single line.
        await make_order("SVC-DET", total=Decimal("250.00"))
        service = AdminOrderService(db_session)

        # Act: fetch it by number.
        order, items = await service.get_order("SVC-DET")

        # Assert: the order and its one line come back.
        assert order.number == "SVC-DET", "the requested order is returned"
        assert len(items) == 1, "the order's single line is hydrated"

    async def test_get_unknown_order_raises_not_found(self, db_session):
        """An unknown number raises :class:`OrderNotFoundError` (the 404 guard)."""
        # Arrange: no orders seeded.
        service = AdminOrderService(db_session)

        # Act / Assert: the private ``_require_order`` guard raises.
        with pytest.raises(OrderNotFoundError):
            await service.get_order(_UNKNOWN_NUMBER)


class TestAdminOrderServiceTransition:
    """``AdminOrderService.transition`` — state machine + audit + guards."""

    async def test_transition_legal_move_writes_history_and_mutates(
        self, db_session, make_order
    ):
        """A legal status + payment move mutates the order and writes history."""
        # Arrange: a fresh order eligible for new→confirmed and pending→paid.
        order = await make_order("SVC-OK", status="new", payment_status="pending")
        service = AdminOrderService(db_session)

        # Act: move both axes in one call.
        updated, items = await service.transition(
            "SVC-OK", to_status="confirmed", to_payment_status="paid"
        )

        # Assert: both axes moved, lines returned, and two history rows written.
        assert updated.status == "confirmed", "fulfilment axis moved"
        assert updated.payment_status == "paid", "payment axis moved"
        assert len(items) == 1, "the order's line is returned with the update"
        history = await OrderRepository(db_session).count_status_history(order.id)
        assert history == 2, "one audit row per moved axis is written"

    async def test_transition_illegal_move_raises_and_writes_no_history(
        self, db_session, make_order
    ):
        """An illegal move raises before mutation and leaves no audit row."""
        # Arrange: a new order (new→done is not an allowed jump).
        order = await make_order("SVC-BAD", status="new")
        service = AdminOrderService(db_session)

        # Act / Assert: the reused machine rejects the jump.
        with pytest.raises(IllegalTransitionError):
            await service.transition("SVC-BAD", to_status="done")

        # Assert: nothing was recorded for the rejected move.
        history = await OrderRepository(db_session).count_status_history(order.id)
        assert history == _NO_HISTORY_ROWS, "an illegal move writes no audit row"

    async def test_transition_unknown_order_raises_not_found(self, db_session):
        """Transitioning an unknown number raises the not-found guard, not a move."""
        # Arrange: no orders seeded.
        service = AdminOrderService(db_session)

        # Act / Assert: ``_require_order`` raises before any machine call.
        with pytest.raises(OrderNotFoundError):
            await service.transition(_UNKNOWN_NUMBER, to_status="confirmed")
