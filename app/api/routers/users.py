"""User profile + address routes, all behind ``current_user`` (§4, §7).

The router carries no ``/api/v1`` prefix (mounted by ``main.py``).
"""

from fastapi import APIRouter, Depends, status

from app.api.deps import current_user, get_auth_service
from app.models.user import AppUser
from app.schemas.auth import (
    AddressCreate,
    AddressOut,
    AddressUpdate,
    UserMe,
    UserUpdate,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserMe)
async def get_me(user: AppUser = Depends(current_user)) -> UserMe:
    """Return the authenticated user's profile."""
    return UserMe.model_validate(user)


@router.patch("/me", response_model=UserMe)
async def update_me(
    data: UserUpdate,
    user: AppUser = Depends(current_user),
    service: AuthService = Depends(get_auth_service),
) -> UserMe:
    """Apply a partial update to the authenticated user's profile."""
    updated = await service.update_me(user.id, data)
    return UserMe.model_validate(updated)


@router.get("/me/addresses", response_model=list[AddressOut])
async def list_addresses(
    user: AppUser = Depends(current_user),
    service: AuthService = Depends(get_auth_service),
) -> list[AddressOut]:
    """List the authenticated user's addresses."""
    addresses = await service.list_addresses(user.id)
    return [AddressOut.model_validate(address) for address in addresses]


@router.post(
    "/me/addresses",
    response_model=AddressOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_address(
    data: AddressCreate,
    user: AppUser = Depends(current_user),
    service: AuthService = Depends(get_auth_service),
) -> AddressOut:
    """Create a new address (keeps at most one default per user)."""
    address = await service.create_address(user.id, data)
    return AddressOut.model_validate(address)


@router.patch("/me/addresses/{address_id}", response_model=AddressOut)
async def update_address(
    address_id: int,
    data: AddressUpdate,
    user: AppUser = Depends(current_user),
    service: AuthService = Depends(get_auth_service),
) -> AddressOut:
    """Update one of the authenticated user's addresses."""
    address = await service.update_address(user.id, address_id, data)
    return AddressOut.model_validate(address)


@router.delete(
    "/me/addresses/{address_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_address(
    address_id: int,
    user: AppUser = Depends(current_user),
    service: AuthService = Depends(get_auth_service),
) -> None:
    """Delete one of the authenticated user's addresses."""
    await service.delete_address(user.id, address_id)
