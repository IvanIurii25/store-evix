"""Celery tasks for restock notifications (Phase 1, §4).

* ``restock.notify`` — fired after a product transitions 0 → in-stock; emails the
  active subscribers. Durable: retries with backoff so a transient SMTP/DB blip
  doesn't drop the batch.
* ``restock.sweep`` — path-independent reconciliation: notifies every product
  that has active subscribers AND is currently in stock, catching any stock
  growth that bypassed ``update_product``. (Beat scheduling is Phase 3.)

Both use :func:`app.tasks.base.run_async_session` (a fresh NullPool engine on a
fresh event loop per call — fork-safe) and delegate the actual work to
:meth:`RestockService.notify_product`.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.celery_app import celery_app
from app.services.restock_service import RestockService
from app.tasks.base import run_async_session


async def _notify_product(session: AsyncSession, product_id: int) -> int:
    """Notify active subscribers of one restocked product.

    Args:
        session: Task-scoped async session.
        product_id: The product that returned to stock.

    Returns:
        int: The number of subscribers notified.
    """
    return await RestockService(session).notify_product(session, product_id)


async def _sweep(session: AsyncSession) -> int:
    """Notify every product that has active subscribers and is in stock.

    Args:
        session: Task-scoped async session.

    Returns:
        int: The total number of subscribers notified across all products.
    """
    service = RestockService(session)
    product_ids = await service.repo.products_with_active_and_in_stock()
    total = 0
    for product_id in product_ids:
        total += await service.notify_product(session, product_id)
    return total


@celery_app.task(
    name="restock.notify",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def send_restock_notifications(product_id: int) -> int:
    """Email the active subscribers of a restocked product (durable, retried).

    Args:
        product_id: The product that returned to stock.

    Returns:
        int: The number of subscribers notified.
    """
    return run_async_session(lambda session: _notify_product(session, product_id))


@celery_app.task(name="restock.sweep")
def sweep_restocked() -> int:
    """Reconciliation sweep: notify all in-stock products with active waiters.

    Returns:
        int: The total number of subscribers notified.
    """
    return run_async_session(_sweep)
