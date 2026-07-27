"""Concurrency: two admins race to refund the same paid card order (§card).

Refund reads ``order.payment_status == "paid"``, calls maib over HTTP, then marks
the order refunded. Two concurrent refunds would both pass the unlocked guard and
both call maib — refunding twice at the provider. The fix loads the order
``FOR UPDATE`` for the whole refund (guard + maib call + transition), so the
second refund blocks, re-reads the now-``refunded`` status, and is rejected before
it can call maib again.

Opts out of SAVEPOINT isolation like the other races: two independent engines run
two real refunds concurrently against a shared maib stub that counts calls. A
small delay in the stub widens the window so the pre-fix path would double-call.
"""

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.order import Order
from app.models.payment import Payment
from app.services.admin_order_service import AdminOrderService
from app.services.payment.payment_service import PaymentService, RefundNotAllowedError
from tests.payment.conftest import StubMaibClient

pytestmark = pytest.mark.asyncio

_NUMBER = "20260727-000900"


class _SlowStub(StubMaibClient):
    """Maib stub whose refund takes a beat — widens the race window."""

    async def refund(self, pay_id: str, amount: float | None = None) -> dict:
        await asyncio.sleep(0.05)
        return await super().refund(pay_id, amount)


async def _seed(factory: async_sessionmaker) -> None:
    """Commit one paid card order with its ``ok`` payment (has a pay_id)."""
    async with factory() as session:
        session.add(
            Order(
                id=1,
                number=_NUMBER,
                email="refund@example.com",
                phone="+37360000010",
                status="confirmed",
                payment_status="paid",
                subtotal=Decimal("100.00"),
                total=Decimal("100.00"),
                delivery_type="pickup",
                payment_method="card",
            )
        )
        await session.flush()
        session.add(
            Payment(
                order_id=1,
                provider="maib",
                pay_id="PAY-1",
                status="ok",
                amount=Decimal("100.00"),
            )
        )
        await session.commit()


async def _refund(factory: async_sessionmaker, stub: StubMaibClient) -> None:
    """Load the order locked (as the router does) and refund it."""
    async with factory() as session:
        order, _ = await AdminOrderService(session).get_order(_NUMBER, for_update=True)
        await PaymentService(session, client=stub).refund(order)


async def test_two_concurrent_refunds_call_maib_once(real_engine) -> None:
    """One refund wins; the other is rejected and maib is charged exactly once."""
    _, factory_seed = real_engine()
    await _seed(factory_seed)

    stub = _SlowStub()  # shared across both refunds — counts total maib calls
    _, factory_a = real_engine()
    _, factory_b = real_engine()

    results = await asyncio.gather(
        _refund(factory_a, stub),
        _refund(factory_b, stub),
        return_exceptions=True,
    )

    successes = [r for r in results if r is None]
    rejections = [r for r in results if isinstance(r, RefundNotAllowedError)]
    assert len(successes) == 1, f"expected one refund, got results={results}"
    assert len(rejections) == 1, f"expected one rejection, got results={results}"
    assert len(stub.refund_calls) == 1, f"maib called {len(stub.refund_calls)}x"

    async with factory_seed() as session:
        order = (
            await session.execute(select(Order).where(Order.id == 1))
        ).scalar_one()
        assert order.payment_status == "refunded"
