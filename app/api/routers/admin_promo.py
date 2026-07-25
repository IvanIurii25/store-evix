"""Admin promo-code API — coupon CRUD (feature A1, §2.5).

Thin back-office router entirely behind ``Depends(current_staff)`` (JWT +
``is_staff``; a non-staff caller gets 403 from the dependency). It delegates to
:class:`~app.services.promo_service.PromoService` and maps domain errors to HTTP
status codes via the unified error envelope. No business logic and no SQL here.

Endpoints (the ``/api/v1`` prefix is added by the integrator when mounting):

* ``GET    /admin/promo``        — the coupon roster.
* ``POST   /admin/promo``        — create a coupon.
* ``GET    /admin/promo/{id}``   — one coupon.
* ``PATCH  /admin/promo/{id}``   — partial update.
* ``DELETE /admin/promo/{id}``   — delete a coupon.
"""

from fastapi import APIRouter, Depends, HTTPException, status
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
from app.services.promo_service import (
    PromoConflictError,
    PromoNotFoundError,
    PromoService,
    PromoValidationError,
)

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
    """Create a coupon."""
    try:
        promo = await PromoService(session).create_promo(payload)
    except PromoValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except PromoConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return PromoOut.model_validate(promo)


@router.get("/{promo_id}", response_model=PromoOut)
async def get_promo(
    promo_id: int,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> PromoOut:
    """Return one coupon by id."""
    try:
        promo = await PromoService(session).get_promo(promo_id)
    except PromoNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return PromoOut.model_validate(promo)


@router.patch("/{promo_id}", response_model=PromoOut)
async def update_promo(
    promo_id: int,
    payload: PromoUpdate,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> PromoOut:
    """Apply a partial update to a coupon."""
    try:
        promo = await PromoService(session).update_promo(promo_id, payload)
    except PromoNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except PromoValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except PromoConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return PromoOut.model_validate(promo)


@router.delete("/{promo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_promo(
    promo_id: int,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a coupon (order history keeps its code snapshot)."""
    try:
        await PromoService(session).delete_promo(promo_id)
    except PromoNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
