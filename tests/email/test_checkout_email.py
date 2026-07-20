"""Integration: checkout fires the confirmation email as a post-commit effect.

Exercises the real :class:`CheckoutService` against the commit-safe test session.
The sender is monkeypatched at its import site in ``checkout_service`` so we can
assert (a) it is called after a successful order creation with the right ``to``
and order number, and (b) a sender failure is swallowed — the order still
returns intact.
"""

from decimal import Decimal

import pytest

from app.services import checkout_service
from app.services.checkout_service import CheckoutService

pytestmark = pytest.mark.asyncio


async def test_checkout_triggers_confirmation_with_right_args(
    db_session,
    seed_cart,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful checkout calls the sender with the order's email + number."""
    token = await seed_cart(product_id=1, price=Decimal("100.00"), qty=2)

    calls: list[dict] = []

    async def _spy(**kwargs) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(checkout_service, "send_order_confirmation", _spy)

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
    assert calls[0]["total"] == order.total


async def test_send_failure_does_not_break_the_order(
    db_session,
    seed_cart,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the sender raises, the committed order is still returned (§9.8)."""
    token = await seed_cart(product_id=2, price=Decimal("100.00"), qty=1)

    async def _boom(**_kwargs) -> None:
        raise RuntimeError("smtp exploded")

    monkeypatch.setattr(checkout_service, "send_order_confirmation", _boom)

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
