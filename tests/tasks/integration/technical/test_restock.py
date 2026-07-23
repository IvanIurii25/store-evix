"""Tests for the restock Celery tasks (Phase 1, §4).

The task bodies (``send_restock_notifications`` / ``sweep_restocked``) are thin:
they hand an async op to :func:`run_async_session` (a sync bridge that spins its
own event loop — cannot run inside pytest's loop). So we monkeypatch
``app.tasks.restock.run_async_session`` to *capture* the op and run it against
the transactional ``db_session`` ourselves, asserting the real notify/sweep
behaviour end-to-end. The inner helpers ``_notify_product`` / ``_sweep`` are also
awaited directly for their subscriber-count logic.

Restock emails use the console backend in tests (no SMTP) — we assert notify
counts and the ``notified`` flip rather than SMTP delivery.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.tasks.restock as restock_tasks
from app.core import email
from app.models.restock import STATUS_ACTIVE, STATUS_NOTIFIED
from app.tasks.restock import (
    _notify_product,
    _sweep,
    send_restock_notifications,
    sweep_restocked,
)
from tests.factories import (
    create_product,
    create_restock_subscription,
    create_user,
)

pytestmark = pytest.mark.asyncio

# Stock levels as named constants for AAA readability.
_IN_STOCK_QTY: int = 5
_OUT_OF_STOCK_QTY: int = 0
# Storefront base used to render the console email link.
_STOREFRONT_URL: str = "https://shop.evix.md"


def _run_via_session(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    """Patch ``run_async_session`` to await the op against ``db_session``.

    Returns a recorder so the test can assert the bridge was invoked with a
    callable. The awaitable produced by the op is stored for the test to await
    (the task body is sync, so it cannot await it itself).
    """

    class _Recorder:
        def __init__(self) -> None:
            self.awaitable = None
            self.received_callable = False

        def __call__(self, op):  # noqa: ANN001 — Callable[[AsyncSession], Awaitable]
            self.received_callable = callable(op)
            # Build (not await) the coroutine bound to the test session.
            self.awaitable = op(db_session)
            return self.awaitable

    recorder = _Recorder()
    monkeypatch.setattr(restock_tasks, "run_async_session", recorder)
    return recorder


class TestRestockNotifyInnerHelper:
    """Direct ``_notify_product`` behaviour against the real session (§4/§7)."""

    async def test_notify_product_returns_notified_count(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: two active waiters on an in-stock product with translations.
        monkeypatch.setattr(email.settings, "email_backend", email.BACKEND_CONSOLE)
        monkeypatch.setattr(email.settings, "storefront_base_url", _STOREFRONT_URL)
        product = await create_product(db_session, qty=_IN_STOCK_QTY)
        subs = []
        for idx in range(2):
            waiter = await create_user(db_session, email=f"n{idx}@example.com")
            subs.append(
                await create_restock_subscription(
                    db_session, product=product, user=waiter, lang="ru"
                )
            )

        # Act: notify all active subscribers of the restocked product.
        notified = await _notify_product(db_session, product.id)

        # Assert: both were notified and their rows flipped to notified.
        assert notified == 2, "both active subscribers must be notified"
        for sub in subs:
            await db_session.refresh(sub)
            assert sub.status == STATUS_NOTIFIED, "sent sub must be marked notified"

    async def test_notify_product_no_subscribers_returns_zero(
        self,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: an in-stock product with no waiters.
        product = await create_product(db_session, qty=_IN_STOCK_QTY)

        # Act: notify.
        notified = await _notify_product(db_session, product.id)

        # Assert: nothing to send.
        assert notified == 0, "a product with no waiters notifies nobody"


class TestRestockSweepInnerHelper:
    """Direct ``_sweep`` behaviour: notify every in-stock, actively-waited product."""

    async def test_sweep_notifies_all_in_stock_waited(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: two in-stock products with active waiters + one OOS (excluded).
        monkeypatch.setattr(email.settings, "email_backend", email.BACKEND_CONSOLE)
        monkeypatch.setattr(email.settings, "storefront_base_url", _STOREFRONT_URL)
        prod_a = await create_product(db_session, qty=_IN_STOCK_QTY)
        prod_b = await create_product(db_session, qty=_IN_STOCK_QTY)
        oos = await create_product(db_session, qty=_OUT_OF_STOCK_QTY)
        for idx, product in enumerate((prod_a, prod_b, oos)):
            waiter = await create_user(db_session, email=f"s{idx}@example.com")
            await create_restock_subscription(
                db_session, product=product, user=waiter, lang="ru"
            )

        # Act: run the reconciliation sweep.
        total = await _sweep(db_session)

        # Assert: only the two in-stock products' waiters were notified.
        assert total == 2, "sweep must notify only in-stock, active waiters"

    async def test_sweep_no_candidates_returns_zero(
        self,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: an active waiter but the product is OUT of stock.
        product = await create_product(db_session, qty=_OUT_OF_STOCK_QTY)
        waiter = await create_user(db_session)
        await create_restock_subscription(
            db_session, product=product, user=waiter, lang="ru"
        )

        # Act: sweep finds no in-stock candidates.
        total = await _sweep(db_session)

        # Assert: nothing notified.
        assert total == 0, "no in-stock candidate means nothing to sweep"


class TestRestockTaskBodies:
    """Task bodies delegate to the bridge with a callable op (§4)."""

    async def test_send_restock_notifications_bridges_notify(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: an in-stock product with one active waiter + console email.
        monkeypatch.setattr(email.settings, "email_backend", email.BACKEND_CONSOLE)
        monkeypatch.setattr(email.settings, "storefront_base_url", _STOREFRONT_URL)
        product = await create_product(db_session, qty=_IN_STOCK_QTY)
        waiter = await create_user(db_session)
        sub = await create_restock_subscription(
            db_session, product=product, user=waiter, lang="ru"
        )
        recorder = _run_via_session(db_session, monkeypatch)

        # Act: the sync task body builds the op and passes it to the bridge.
        awaitable = send_restock_notifications(product.id)
        notified = await awaitable

        # Assert: the bridge got a callable and the op notified the waiter.
        assert recorder.received_callable is True, "bridge op must be callable"
        assert notified == 1, "the single active waiter must be notified"
        await db_session.refresh(sub)
        assert sub.status == STATUS_NOTIFIED, "waiter must be marked notified"

    async def test_sweep_restocked_bridges_sweep(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: one in-stock, actively-waited product + console email.
        monkeypatch.setattr(email.settings, "email_backend", email.BACKEND_CONSOLE)
        monkeypatch.setattr(email.settings, "storefront_base_url", _STOREFRONT_URL)
        product = await create_product(db_session, qty=_IN_STOCK_QTY)
        waiter = await create_user(db_session)
        await create_restock_subscription(
            db_session, product=product, user=waiter, lang="ru"
        )
        recorder = _run_via_session(db_session, monkeypatch)

        # Act: the sync sweep task body passes its op to the bridge.
        awaitable = sweep_restocked()
        total = await awaitable

        # Assert: the bridge got a callable and the sweep notified the waiter.
        assert recorder.received_callable is True, "bridge op must be callable"
        assert total == 1, "the in-stock waited product must notify one waiter"

    async def test_sweep_restocked_no_candidates_returns_zero(
        self,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: an OOS product with an active waiter (no sweep candidate).
        product = await create_product(db_session, qty=_OUT_OF_STOCK_QTY)
        waiter = await create_user(db_session)
        active_sub = await create_restock_subscription(
            db_session, product=product, user=waiter, lang="ru"
        )
        _run_via_session(db_session, monkeypatch)

        # Act: run the sweep task body.
        total = await sweep_restocked()

        # Assert: nothing to notify; the waiter stays active.
        assert total == 0, "no in-stock candidate means the sweep notifies nobody"
        await db_session.refresh(active_sub)
        assert active_sub.status == STATUS_ACTIVE, "OOS waiter stays active"
