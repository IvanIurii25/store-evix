"""Admin settings API — SEO defaults + staff management (§6.4).

Thin back-office router entirely behind ``Depends(current_staff)`` (JWT +
``is_staff``; a non-staff caller gets 403 from the dependency). It delegates to
:class:`~app.services.admin_settings_service.SettingsService` (SEO) and
:class:`~app.services.admin_settings_service.StaffService` (roster), and maps
domain errors to HTTP status codes. No business logic and no SQL live here.

Endpoints (the ``/api/v1`` prefix is added by the integrator when mounting):

* ``GET  /admin/settings/seo`` / ``PUT /admin/settings/seo`` — site SEO defaults.
* ``GET  /admin/staff`` — the staff roster.
* ``POST /admin/staff`` — create a staff user (or promote + reset by email).
* ``PATCH /admin/staff/{user_id}`` — (de)activate / toggle the staff flag.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.core.db import get_session
from app.models.user import AppUser
from app.schemas.admin_settings import (
    SeoSettings,
    StaffCreate,
    StaffItem,
    StaffList,
    StaffUpdate,
)
from app.services.admin_settings_service import (
    SettingsService,
    StaffConflictError,
    StaffNotFoundError,
    StaffService,
)

router = APIRouter(
    prefix="/admin",
    tags=["admin-settings"],
    dependencies=[Depends(current_staff)],
)


# --------------------------------------------------------------------------- #
# SEO defaults
# --------------------------------------------------------------------------- #
@router.get("/settings/seo", response_model=SeoSettings)
async def get_seo(
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> SeoSettings:
    """Return the site-wide SEO defaults."""
    return await SettingsService(session).get_seo()


@router.put("/settings/seo", response_model=SeoSettings)
async def put_seo(
    payload: SeoSettings,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> SeoSettings:
    """Persist the site-wide SEO defaults and return the stored block."""
    return await SettingsService(session).put_seo(payload)


# --------------------------------------------------------------------------- #
# Staff management
# --------------------------------------------------------------------------- #
@router.get("/staff", response_model=StaffList)
async def list_staff(
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> StaffList:
    """Return the staff roster (newest first)."""
    users = await StaffService(session).list_staff()
    return StaffList(data=[StaffItem.model_validate(user) for user in users])


@router.post(
    "/staff",
    response_model=StaffItem,
    status_code=status.HTTP_201_CREATED,
)
async def create_staff(
    payload: StaffCreate,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> StaffItem:
    """Create a new staff user, or promote + reset an existing one by email."""
    user = await StaffService(session).create_or_promote(
        email=payload.email,
        password=payload.password,
        phone=payload.phone,
    )
    return StaffItem.model_validate(user)


@router.patch("/staff/{user_id}", response_model=StaffItem)
async def update_staff(
    user_id: int,
    payload: StaffUpdate,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> StaffItem:
    """(De)activate a staff user or toggle their staff flag (lockout-guarded)."""
    try:
        user = await StaffService(session).update_staff(
            user_id,
            is_active=payload.is_active,
            is_staff=payload.is_staff,
        )
    except StaffNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": str(exc)},
        ) from exc
    except StaffConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "conflict", "message": str(exc)},
        ) from exc
    return StaffItem.model_validate(user)
