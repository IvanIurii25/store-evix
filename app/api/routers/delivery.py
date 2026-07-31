"""Public delivery endpoints: available methods + Nova Post reference data.

``GET /delivery/methods`` is the single source of truth for what the checkout
may offer, so the storefront never hardcodes the option list. The two lookups
back the city / pickup-point pickers.

All three are public (guests check out too) and rate-limited per IP: they proxy
a third-party API, and without a budget our endpoint would be a free front door
to it. Results are cached in the service layer.

The lookups are shut with 404 while the carrier is off, so a disabled
integration exposes no surface at all.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis

from app.api.lang import get_lang
from app.api.ratelimit import rate_limiter
from app.core.config import settings
from app.core.redis import get_redis
from app.schemas.delivery import (
    DeliveryMethodsOut,
    DivisionListOut,
    DivisionQuery,
    SettlementListOut,
    SettlementQuery,
)
from app.services.delivery.novapost_client import NovaPostError
from app.services.delivery.novapost_service import NovaPostService

router = APIRouter(prefix="/delivery", tags=["delivery"])

_RATE_LIMIT = Depends(rate_limiter(settings.rate_limit_delivery, "delivery"))


def _require_enabled() -> None:
    """Reject the request when the carrier is not configured.

    Raises:
        HTTPException: 404 ``novapost_disabled`` — an integration that is off
            should not advertise its endpoints.
    """
    if not settings.novapost_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "novapost_disabled",
                "message": "Nova Post is not available",
            },
        )


def _carrier_unavailable(exc: NovaPostError) -> HTTPException:
    """Translate a carrier failure into a 502 the storefront can act on."""
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "code": "delivery_lookup_unavailable",
            "message": "Delivery service is temporarily unavailable",
            "details": {"reason": str(exc)},
        },
    )


@router.get("/methods", response_model=DeliveryMethodsOut)
async def list_methods(redis: Redis = Depends(get_redis)) -> DeliveryMethodsOut:
    """Return the delivery methods the storefront may offer.

    Args:
        redis: Injected Redis client (carrier token / cache).

    Returns:
        DeliveryMethodsOut: Own methods always, carrier methods when enabled.
    """
    return NovaPostService(redis).methods()


@router.post(
    "/novapost/settlements",
    response_model=SettlementListOut,
    dependencies=[_RATE_LIMIT],
)
async def search_settlements(
    payload: SettlementQuery,
    lang: str = Depends(get_lang),
    redis: Redis = Depends(get_redis),
) -> SettlementListOut:
    """Search carrier settlements (cities) for the checkout picker.

    Args:
        payload: The typed query.
        lang: Resolved request language (localizes the names).
        redis: Injected Redis client.

    Returns:
        SettlementListOut: Matching settlements.

    Raises:
        HTTPException: 404 when the carrier is off, 502 when it is unreachable.
    """
    _require_enabled()
    try:
        rows = await NovaPostService(redis, lang=lang).settlements(payload.query)
    except NovaPostError as exc:
        raise _carrier_unavailable(exc) from exc
    return SettlementListOut(data=rows)


@router.post(
    "/novapost/divisions",
    response_model=DivisionListOut,
    dependencies=[_RATE_LIMIT],
)
async def search_divisions(
    payload: DivisionQuery,
    lang: str = Depends(get_lang),
    redis: Redis = Depends(get_redis),
) -> DivisionListOut:
    """List branches or postomats inside a settlement.

    Args:
        payload: Settlement id, category and optional free text.
        lang: Resolved request language.
        redis: Injected Redis client.

    Returns:
        DivisionListOut: Matching pickup points.

    Raises:
        HTTPException: 404 when the carrier is off, 502 when it is unreachable.
    """
    _require_enabled()
    try:
        rows = await NovaPostService(redis, lang=lang).divisions(
            payload.settlement_id, payload.category, payload.query
        )
    except NovaPostError as exc:
        raise _carrier_unavailable(exc) from exc
    return DivisionListOut(data=rows)
