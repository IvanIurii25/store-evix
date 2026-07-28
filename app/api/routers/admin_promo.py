"""Admin promo-code API — coupon CRUD (feature A1, §2.5).

Thin back-office router entirely behind ``Depends(current_staff)`` (JWT +
``is_staff``; a non-staff caller gets 403 from the dependency). It delegates to
:class:`~app.services.promo_service.PromoService`; the promo domain errors it
raises are :class:`DomainError` subclasses rendered by the app's registered
handler into the unified ``{error:{code,message}}`` envelope (each leaf carries
its own status + code), so the router does not catch them. No business logic and
no SQL here.

Endpoints (the ``/api/v1`` prefix is added by the integrator when mounting):

* ``GET    /admin/promo``        — the coupon roster.
* ``POST   /admin/promo``        — create a coupon.
* ``GET    /admin/promo/{id}``   — one coupon.
* ``PATCH  /admin/promo/{id}``   — partial update.
* ``DELETE /admin/promo/{id}``   — delete a coupon.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.core.db import get_session
from app.models.user import AppUser
from app.schemas.admin_promo import (
    PromoCreate,
    PromoList,
    PromoOut,
    PromoUpdate,
)
from app.services.promo_service import PromoService

router = APIRouter(
    prefix="/admin/promo",
    tags=["admin-promo"],
    dependencies=[Depends(current_staff)],
)


@router.get("", response_model=PromoList)
async def list_promos(
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> PromoList:
    """Return the coupon roster (newest first)."""
    promos = await PromoService(session).list_promos()
    return PromoList(data=[PromoOut.model_validate(promo) for promo in promos])


@router.post("", response_model=PromoOut, status_code=status.HTTP_201_CREATED)
async def create_promo(
    payload: PromoCreate,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> PromoOut:
    """Create a coupon.

    Raises:
        PromoValidationError: 422 ``validation_error`` for an inconsistent
            definition.
        PromoConflictError: 409 ``conflict`` when the code already exists. Both
            are rendered by the registered :class:`DomainError` handler.
    """
    promo = await PromoService(session).create_promo(payload)
    return PromoOut.model_validate(promo)


@router.get("/{promo_id}", response_model=PromoOut)
async def get_promo(
    promo_id: int,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> PromoOut:
    """Return one coupon by id.

    Raises:
        PromoNotFoundError: 404 ``not_found`` when no such coupon exists
            (rendered by the registered :class:`DomainError` handler).
    """
    promo = await PromoService(session).get_promo(promo_id)
    return PromoOut.model_validate(promo)


@router.patch("/{promo_id}", response_model=PromoOut)
async def update_promo(
    promo_id: int,
    payload: PromoUpdate,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> PromoOut:
    """Apply a partial update to a coupon.

    Raises:
        PromoNotFoundError: 404 ``not_found`` when the coupon does not exist.
        PromoValidationError: 422 ``validation_error`` for an inconsistent
            resulting definition.
        PromoConflictError: 409 ``conflict`` when a new code collides. All are
            rendered by the registered :class:`DomainError` handler.
    """
    promo = await PromoService(session).update_promo(promo_id, payload)
    return PromoOut.model_validate(promo)


@router.delete("/{promo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_promo(
    promo_id: int,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a coupon (order history keeps its code snapshot).

    Raises:
        PromoNotFoundError: 404 ``not_found`` when the coupon does not exist
            (rendered by the registered :class:`DomainError` handler).
    """
    await PromoService(session).delete_promo(promo_id)
