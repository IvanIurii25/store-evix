"""Integration: checkout enqueues the confirmation email as a post-commit effect.

Exercises the real :class:`CheckoutService` against the commit-safe test session.
The confirmation email is no longer sent inline — the service now enqueues the
durable Celery task ``order.confirm_email``. We patch that task's ``.delay`` at
its import site in ``checkout_service`` so we can assert (a) it is enqueued after a
successful order creation with the right ``to``, order number and string total,
and (b) an enqueue failure is swallowed — the order still returns intact.
"""

from decimal import Decimal

import pytest

from app.services import checkout_service
from app.services.checkout_service import CheckoutService

pytestmark = pytest.mark.asyncio


async def test_checkout_enqueues_confirmation_with_right_args(
    db_session,
    seed_cart,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful checkout enqueues the task with email, number + string total."""
    token = await seed_cart(product_id=1, price=Decimal("100.00"), qty=2)

    calls: list[dict] = []

    def _spy_delay(**kwargs) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(
        checkout_service.send_order_confirmation_email, "delay", _spy_delay
    )

    service = CheckoutService(db_session)
    order = await service.checkout(
        user_id=None,
        session_token=token,
        email="buyer@example.com",
        phone="+37360000000",
        delivery_type="pickup",
        delivery_address_id=None,
    )

    assert len(calls) == 1
    assert calls[0]["to"] == "buyer@example.com"
    assert calls[0]["order_number"] == order.number
    assert calls[0]["total_str"] == str(order.total)


async def test_enqueue_failure_does_not_break_the_order(
    db_session,
    seed_cart,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the enqueue raises (e.g. broker down), the committed order still returns."""
    token = await seed_cart(product_id=2, price=Decimal("100.00"), qty=1)

    def _boom(**_kwargs) -> None:
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(
        checkout_service.send_order_confirmation_email, "delay", _boom
    )

    service = CheckoutService(db_session)
    order = await service.checkout(
        user_id=None,
        session_token=token,
        email="buyer2@example.com",
        phone="+37360000001",
        delivery_type="pickup",
        delivery_address_id=None,
    )

    assert order.number
    assert order.total == Decimal("100.00")
