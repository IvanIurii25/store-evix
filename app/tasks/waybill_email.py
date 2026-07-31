"""Celery task announcing a created waybill to the customer (phase P5).

Split from the order-confirmation task because it fires from a different place
(an operator creating the shipment, possibly days later) and carries different
content: the tracking number.

Like every other mail task it is fire-and-forget from the caller's point of
view — the send happens after the waybill is already committed, so a mail
outage can never undo a shipment that exists at the carrier.
"""

from app.core.celery_app import celery_app
from app.core.email import send_waybill_created
from app.tasks.base import run_async_session


@celery_app.task(
    name="novapost.waybill_email",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def send_waybill_email(
    *,
    to: str,
    order_number: str,
    awb_number: str,
    destination: str = "",
) -> None:
    """Email the customer their tracking number.

    Args:
        to: Recipient contact email.
        order_number: The order the shipment belongs to.
        awb_number: The carrier waybill number.
        destination: Human-readable pickup point or address.
    """
    # Same runner as the other mail tasks (fresh loop per call, fork-safe),
    # even though sending does no DB work.
    return run_async_session(
        lambda _session: send_waybill_created(
            to=to,
            order_number=order_number,
            awb_number=awb_number,
            destination=destination,
        )
    )
