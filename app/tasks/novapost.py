"""Celery task keeping Nova Post shipment statuses fresh (phase P5).

``novapost.sync_statuses`` asks the carrier where every open parcel is and
stores the answer on the order. It is the only way a customer or an operator
learns that a shipment moved: the carrier does not call us back.

Only **open** waybills are polled — a delivered or returned parcel never changes
again, so re-asking about it would grow the sweep forever as order history does.
The partial index ``ix_order_delivery_np_open_awb`` matches that access pattern.

Statuses are stored as a machine-readable ``status_code`` plus the carrier's own
``status_text``. ``ecom-obr`` concatenates the two into one formatted string,
which cannot be filtered or reasoned about.
"""

import logging
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.celery_app import celery_app
from app.core.config import settings
from app.models.order import OrderDeliveryNovaPost
from app.services.delivery.novapost_client import NovaPostError
from app.services.delivery.novapost_service import NovaPostService
from app.tasks.base import run_async_session

logger = logging.getLogger(__name__)

# Statuses after which a parcel never moves again (compared case-insensitively
# against the carrier's own wording). Anything else is still in flight.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"delivered", "returned", "lost", "cancelled", "canceled"}
)

# One carrier request carries this many waybills, so a large backlog costs a
# handful of round-trips rather than one per parcel.
_BATCH: int = 100


def _is_terminal(status_text: str) -> bool:
    """Return whether a carrier status means the parcel has finished moving."""
    return status_text.strip().lower() in TERMINAL_STATUSES


async def _sync(session: AsyncSession) -> int:
    """Refresh the status of every open waybill.

    Args:
        session: Task-scoped async session.

    Returns:
        int: How many rows had their status changed.
    """
    if not settings.novapost_enabled:
        # The carrier was switched off; nothing to ask and nobody to ask.
        return 0

    rows = list(
        (
            await session.execute(
                select(OrderDeliveryNovaPost).where(
                    OrderDeliveryNovaPost.awb_number.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    open_rows = [row for row in rows if not _is_terminal(row.status_text)]
    if not open_rows:
        return 0

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    service = NovaPostService(redis)
    changed = 0
    try:
        for start in range(0, len(open_rows), _BATCH):
            batch = open_rows[start : start + _BATCH]
            numbers = [row.awb_number for row in batch if row.awb_number]
            try:
                statuses = await service.tracking(numbers)
            except NovaPostError as exc:
                # A carrier outage costs this sweep, not the next one: the rows
                # stay open and are retried in 30 minutes.
                logger.warning("novapost: tracking sweep failed (%s)", exc)
                continue
            for row in batch:
                answer = statuses.get(row.awb_number or "")
                if not answer:
                    continue
                code, text = answer
                if code == row.status_code and text == row.status_text:
                    continue
                row.status_code = code
                row.status_text = text
                row.status_updated_at = datetime.now(UTC)
                changed += 1
        await session.commit()
    finally:
        await redis.aclose()
    return changed


@celery_app.task(name="novapost.sync_statuses")
def sync_shipment_statuses() -> int:
    """Poll the carrier for every open waybill and store what changed.

    Returns:
        int: The number of shipments whose status changed.
    """
    return run_async_session(_sync)
