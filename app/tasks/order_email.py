"""Celery task for the order-confirmation email (checkout hot-path offload, §9.8).

* ``order.confirm_email`` — fired after an order is committed; renders and sends
  the confirmation email off the request path. Durable: retries with backoff so a
  transient SMTP blip doesn't drop the message.

The task accepts ``total`` as a STRING because Celery serializes task args as JSON
over the Redis broker and :class:`~decimal.Decimal` is not JSON-serializable; the
``Decimal`` is reconstructed inside the task before rendering. It uses
:func:`app.tasks.base.run_async_session` (a fresh NullPool engine on a fresh event
loop per call — fork-safe) to keep the async-runner pattern consistent with the
other task modules, even though the confirmation send does no DB work.
"""

from decimal import Decimal

from app.core.celery_app import celery_app
from app.core.email import send_order_confirmation
from app.tasks.base import run_async_session


@celery_app.task(
    name="order.confirm_email",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def send_order_confirmation_email(
    to: str,
    order_number: str,
    total_str: str,
    delivery: str = "",
) -> None:
    """Send the order-confirmation email off the request path (durable, retried).

    Args:
        to: Recipient contact email.
        order_number: The created order number.
        total_str: The order grand total as a decimal string (JSON-safe over the
            broker); reconstructed to :class:`~decimal.Decimal` before rendering.
    """
    return run_async_session(
        lambda session: send_order_confirmation(
            to=to,
            order_number=order_number,
            total=Decimal(total_str),
            delivery=delivery,
        )
    )
