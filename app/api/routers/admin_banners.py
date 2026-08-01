"""Admin banners API (homepage carousel CRUD, P0).

Thin back-office router entirely behind :func:`~app.api.deps.current_staff`.
Validation lives in the Pydantic schemas, business rules in
:class:`~app.services.banner_service.BannerService`; its domain errors are
rendered into the unified envelope by the registered handler. No SQL here.

Creatives are uploaded through the existing ``POST /admin/assets`` (which already
validates the image and generates the webp variants) — this router only stores
the resulting URLs.

Prefix is ``/admin/banners`` (the integrator mounts the router under ``/api/v1``).
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.core.db import get_session
from app.models.banner import Banner
from app.models.user import AppUser
from app.schemas.banner import (
    BannerAdminOut,
    BannerCreate,
    BannerReorderRequest,
    BannerUpdate,
)
from app.services.banner_service import BannerService

router = APIRouter(
    prefix="/admin/banners",
    tags=["admin-banners"],
    dependencies=[Depends(current_staff)],
)


def _to_out(banner: Banner) -> BannerAdminOut:
    """Build the admin response DTO from a loaded banner."""
    return BannerAdminOut.model_validate(banner)


@router.get("", response_model=list[BannerAdminOut])
async def list_banners(
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> list[BannerAdminOut]:
    """Return every banner — active, scheduled or expired — in display order."""
    banners = await BannerService(session).list_all()
    return [_to_out(banner) for banner in banners]


@router.post("", response_model=BannerAdminOut, status_code=status.HTTP_201_CREATED)
async def create_banner(
    payload: BannerCreate,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> BannerAdminOut:
    """Create a banner with both-language creatives."""
    banner = await BannerService(session).create(payload)
    return _to_out(banner)


@router.get("/{banner_id}", response_model=BannerAdminOut)
async def get_banner(
    banner_id: int,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> BannerAdminOut:
    """Return one banner with both creatives (404 when unknown)."""
    banner = await BannerService(session).get(banner_id)
    return _to_out(banner)


@router.put("/{banner_id}", response_model=BannerAdminOut)
async def update_banner(
    banner_id: int,
    payload: BannerUpdate,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> BannerAdminOut:
    """Replace a banner's schedule and both creatives (404 when unknown)."""
    banner = await BannerService(session).update(banner_id, payload)
    return _to_out(banner)


@router.post("/reorder", response_model=list[BannerAdminOut])
async def reorder_banners(
    payload: BannerReorderRequest,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> list[BannerAdminOut]:
    """Apply new display positions in one write (404 if any id is unknown)."""
    banners = await BannerService(session).reorder(payload)
    return [_to_out(banner) for banner in banners]


@router.delete("/{banner_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_banner(
    banner_id: int,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a banner and its creatives (404 when unknown)."""
    await BannerService(session).delete(banner_id)
