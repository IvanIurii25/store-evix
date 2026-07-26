"""Customer restock-notification router (Phase 1, §5).

Thin router over :class:`~app.services.restock_service.RestockService`. Every
endpoint requires an authenticated user (:func:`~app.api.deps.current_user` →
401 for guests; the storefront reads that 401 as "guest" and redirects to login
with a post-login intent).

* ``POST   /restock/subscriptions``            — subscribe (400 in-stock, 404 missing).
* ``DELETE /restock/subscriptions/{product_id}`` — unsubscribe (204).
* ``GET    /restock/subscriptions/{product_id}`` — button state (``{subscribed}``).
* ``GET    /restock/subscriptions``            — the caller's active waits.

No business logic and no SQL live here. Mounted without the ``/api/v1`` prefix —
the integrator adds it.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.db import get_session
from app.models.user import AppUser
from app.schemas.restock import (
    RestockSubscribedOut,
    RestockSubscribeIn,
    RestockSubscriptionItem,
)
from app.services.restock_service import (
    ProductInStockError,
    RestockNotFoundError,
    RestockService,
    RestockUnsupportedError,
)

# Default storefront language when the ``lang`` query param is omitted.
_DEFAULT_LANG: str = "ro"

router = APIRouter(prefix="/restock", tags=["restock"])


def get_restock_service(
    session: AsyncSession = Depends(get_session),
) -> RestockService:
    """Construct the restock service bound to the request session."""
    return RestockService(session=session)


@router.post("/subscriptions", response_model=RestockSubscribedOut)
async def subscribe(
    payload: RestockSubscribeIn,
    user: AppUser = Depends(current_user),
    service: RestockService = Depends(get_restock_service),
) -> RestockSubscribedOut:
    """Subscribe the current user to a product's restock notification (§5).

    Args:
        payload: Product id + storefront language at subscribe time.
        user: The authenticated user (401 for guests).
        service: Injected restock service.

    Returns:
        RestockSubscribedOut: ``{subscribed: true}``.

    Raises:
        HTTPException: 404 if the product is missing; 400 if it is in stock or
            variable (restock unsupported for variable products in v1).
    """
    try:
        await service.subscribe(payload.product_id, user.id, payload.lang)
    except RestockNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": str(exc)},
        ) from exc
    except RestockUnsupportedError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "unsupported", "message": str(exc)},
        ) from exc
    except ProductInStockError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "in_stock", "message": "товар в наличии"},
        ) from exc
    return RestockSubscribedOut(subscribed=True)


@router.delete(
    "/subscriptions/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unsubscribe(
    product_id: int,
    user: AppUser = Depends(current_user),
    service: RestockService = Depends(get_restock_service),
) -> None:
    """Remove the current user's subscription for a product (idempotent, §5).

    Args:
        product_id: Product to unsubscribe from.
        user: The authenticated user (401 for guests).
        service: Injected restock service.
    """
    await service.unsubscribe(product_id, user.id)


@router.get(
    "/subscriptions/{product_id}",
    response_model=RestockSubscribedOut,
)
async def get_subscription(
    product_id: int,
    user: AppUser = Depends(current_user),
    service: RestockService = Depends(get_restock_service),
) -> RestockSubscribedOut:
    """Return whether the current user is subscribed to a product (§5).

    Args:
        product_id: Product to check.
        user: The authenticated user (401 for guests → button shows "guest").
        service: Injected restock service.

    Returns:
        RestockSubscribedOut: ``{subscribed: bool}``.
    """
    subscribed = await service.is_subscribed(product_id, user.id)
    return RestockSubscribedOut(subscribed=subscribed)


@router.get("/subscriptions", response_model=list[RestockSubscriptionItem])
async def list_subscriptions(
    lang: str = Query(default=_DEFAULT_LANG),
    user: AppUser = Depends(current_user),
    service: RestockService = Depends(get_restock_service),
) -> list[RestockSubscriptionItem]:
    """Return the current user's active "waiting for" items in ``lang`` (§5).

    Args:
        lang: Language to resolve product name/slug in (default ``ro``).
        user: The authenticated user (401 for guests).
        service: Injected restock service.

    Returns:
        list[RestockSubscriptionItem]: ``(product_id, slug, name)`` entries.
    """
    return await service.list_my(user.id, lang)
