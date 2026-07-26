"""Public maib payment webhook (§card-payment).

A single public endpoint, ``POST /payments/maib/callback``, that maib's servers
POST the payment result to. It carries no auth — authenticity is proven by the
payload *signature* alone (verified inside
:class:`~app.services.payment.payment_service.PaymentService`). An invalid or
unknown callback is answered with 200 but changes nothing (maib retries on
non-2xx; we never want a retry storm for a payload we deliberately ignore), while
a valid one settles or cancels the order idempotently.

Mounted without the ``/api/v1`` prefix — the integrator adds it in ``main.py``.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.schemas.payment import MaibCallbackPayload
from app.services.payment.payment_service import PaymentService

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/maib/callback")
async def maib_callback(
    payload: MaibCallbackPayload,
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """Receive and apply a signed maib payment callback (public, idempotent).

    The signature is verified inside the service; a bad signature, an unknown
    ``payId`` or a repeated callback for an already-settled payment all resolve
    to a no-op. We always answer 200 so maib stops retrying — the ``accepted``
    flag tells whether the callback actually moved the order.

    Args:
        payload: The ``{result, signature}`` body maib posted.
        session: Injected async DB session.

    Returns:
        dict[str, bool]: ``{"accepted": <bool>}``.
    """
    service = PaymentService(session)
    accepted = await service.handle_callback(payload.result, payload.signature)
    return {"accepted": accepted}
