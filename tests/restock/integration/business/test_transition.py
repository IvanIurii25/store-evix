"""Transition-trigger test: qty 0 → in-stock enqueues the restock task (§3).

Patches ``send_restock_notifications.delay`` with a spy (simplest — avoids the
``task_always_eager`` + ``asyncio.run`` clash inside pytest's loop) and asserts
:meth:`AdminCatalogService.update_product` enqueues exactly once with the product
id, and only on a genuine ``<=0 → >0`` transition.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.tasks.restock as restock_tasks
from app.schemas.admin_catalog import ProductUpdate
from app.services.admin_catalog_service import AdminCatalogService
from tests.restock.conftest import make_product

pytestmark = pytest.mark.asyncio


class _Spy:
    """Records calls to the patched ``.delay`` in place of the real enqueue."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, product_id: int) -> None:
        self.calls.append(product_id)


async def test_transition_out_to_in_stock_enqueues(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Updating an OOS product to qty>0 enqueues the notification task once."""
    spy = _Spy()
    monkeypatch.setattr(restock_tasks.send_restock_notifications, "delay", spy)
    product = await make_product(db_session, code="trans-1", qty=0)

    await AdminCatalogService(db_session).update_product(
        product.id, ProductUpdate(qty=5)
    )

    assert spy.calls == [product.id]


async def test_no_enqueue_when_still_in_stock(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Updating an already-in-stock product does NOT enqueue (no transition)."""
    spy = _Spy()
    monkeypatch.setattr(restock_tasks.send_restock_notifications, "delay", spy)
    product = await make_product(db_session, code="trans-2", qty=3)

    await AdminCatalogService(db_session).update_product(
        product.id, ProductUpdate(qty=10)
    )

    assert spy.calls == []


async def test_no_enqueue_when_stays_out_of_stock(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editing an OOS product without restocking does NOT enqueue."""
    spy = _Spy()
    monkeypatch.setattr(restock_tasks.send_restock_notifications, "delay", spy)
    product = await make_product(db_session, code="trans-3", qty=0)

    await AdminCatalogService(db_session).update_product(
        product.id, ProductUpdate(price="150")
    )

    assert spy.calls == []
