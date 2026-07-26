"""Tests for the order-confirmation Celery task (checkout offload, §9.8).

The task body (``send_order_confirmation_email``) is thin: it hands an async op to
:func:`run_async_session` (a sync bridge that spins its own event loop — cannot run
inside pytest's loop). So we monkeypatch ``app.tasks.order_email.run_async_session``
to *capture* the op and run it against the transactional ``db_session`` ourselves,
asserting the op reconstructs the ``Decimal`` from its string arg and calls
:func:`send_order_confirmation` with the right kwargs. The DB session handed to the
op is unused by the confirmation send (kept for pattern consistency).
"""

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.tasks.order_email as order_email_task
from app.tasks.order_email import send_order_confirmation_email

pytestmark = pytest.mark.asyncio


async def test_task_reconstructs_decimal_and_calls_sender(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The op rebuilds Decimal(total_str) and calls send_order_confirmation."""
    calls: list[dict] = []

    async def _spy(**kwargs) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(order_email_task, "send_order_confirmation", _spy)

    captured = {}

    def _bridge(op):  # noqa: ANN001 — Callable[[AsyncSession], Awaitable]
        captured["callable"] = callable(op)
        # Build (not await) the coroutine bound to the test session; return it so
        # the test can await it in pytest's own loop.
        return op(db_session)

    monkeypatch.setattr(order_email_task, "run_async_session", _bridge)

    awaitable = send_order_confirmation_email(
        to="buyer@example.com",
        order_number="EVX-0001",
        total_str="149.50",
    )
    await awaitable

    assert captured["callable"] is True, "bridge must receive a callable op"
    assert len(calls) == 1, "the op must call the sender exactly once"
    assert calls[0]["to"] == "buyer@example.com"
    assert calls[0]["order_number"] == "EVX-0001"
    assert calls[0]["total"] == Decimal("149.50")
    assert isinstance(calls[0]["total"], Decimal), "total must be a Decimal"
