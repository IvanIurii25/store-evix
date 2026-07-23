"""Direct service-layer tests for :class:`AuthService` profile + address methods.

Covers ``get_me``/``update_me`` and the address CRUD paths (default handling,
not-found 404s, duplicate-phone conflict) that the API tests leave uncovered.
"""

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Address
from app.schemas.auth import AddressCreate, AddressUpdate, UserUpdate
from app.services.auth_service import AuthService
from tests.factories import create_user, persist

pytestmark = pytest.mark.asyncio

_MISSING_USER_ID = 999_999
_MISSING_ADDRESS_ID = 888_888
_UPDATED_PHONE = "+37362222222"
_EXISTING_PHONE = "+37363333333"


def _service(session: AsyncSession, redis: Redis) -> AuthService:
    """Build a service wired to the transactional session + isolated redis."""
    return AuthService(session=session, redis=redis)


def _address_payload(**overrides) -> AddressCreate:
    """Return a valid AddressCreate, overridable per test."""
    base = {
        "full_name": "Ion Popescu",
        "phone": "+37360000009",
        "city": "Chisinau",
        "street": "Stefan cel Mare 1",
    }
    base.update(overrides)
    return AddressCreate(**base)


class TestProfileReadUpdate:
    """get_me + update_me happy and error branches."""

    async def test_get_me_missing_user_raises_404(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        service = _service(tx_session, redis_client)

        # Act / Assert
        with pytest.raises(Exception) as exc_info:
            await service.get_me(_MISSING_USER_ID)
        assert exc_info.value.status_code == 404, "unknown user id must be 404"

    async def test_update_me_sets_phone(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        user = await create_user(tx_session, email="upd@example.com")
        service = _service(tx_session, redis_client)

        # Act
        updated = await service.update_me(user.id, UserUpdate(phone=_UPDATED_PHONE))

        # Assert
        assert updated.phone == _UPDATED_PHONE, "phone must be applied"

    async def test_update_me_same_phone_skips_dup_check(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange: re-submitting the current phone must not trigger the
        # duplicate lookup (the new_phone != user.phone branch is False).
        user = await create_user(
            tx_session, email="same@example.com", phone=_UPDATED_PHONE
        )
        service = _service(tx_session, redis_client)

        # Act
        updated = await service.update_me(user.id, UserUpdate(phone=_UPDATED_PHONE))

        # Assert
        assert updated.phone == _UPDATED_PHONE, "unchanged phone must be preserved"

    async def test_update_me_duplicate_phone_raises_conflict(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        await create_user(tx_session, email="owner@example.com", phone=_EXISTING_PHONE)
        me = await create_user(tx_session, email="me2@example.com")
        service = _service(tx_session, redis_client)

        # Act / Assert
        with pytest.raises(Exception) as exc_info:
            await service.update_me(me.id, UserUpdate(phone=_EXISTING_PHONE))
        assert exc_info.value.status_code == 409, "another user's phone must be 409"


class TestAddressService:
    """create/update/delete address service branches."""

    async def test_create_default_address_clears_previous_default(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        user = await create_user(tx_session, email="addr1@example.com")
        service = _service(tx_session, redis_client)
        first = await service.create_address(user.id, _address_payload(is_default=True))

        # Act: a second default must demote the first.
        second = await service.create_address(
            user.id, _address_payload(full_name="Maria", is_default=True)
        )

        # Assert
        stmt = select(Address).where(Address.id == first.id)
        refreshed_first = (await tx_session.execute(stmt)).scalar_one()
        assert refreshed_first.is_default is False, "old default must be cleared"
        assert second.is_default is True, "new address must be the default"

    async def test_update_address_promotes_to_default(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        user = await create_user(tx_session, email="addr2@example.com")
        service = _service(tx_session, redis_client)
        default = await service.create_address(
            user.id, _address_payload(is_default=True)
        )
        other = await service.create_address(
            user.id, _address_payload(full_name="B", is_default=False)
        )

        # Act
        promoted = await service.update_address(
            user.id, other.id, AddressUpdate(is_default=True)
        )

        # Assert
        stmt = select(Address).where(Address.id == default.id)
        refreshed_default = (await tx_session.execute(stmt)).scalar_one()
        assert promoted.is_default is True, "target must become the default"
        assert refreshed_default.is_default is False, "old default must be demoted"

    async def test_update_address_applies_plain_field(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        user = await create_user(tx_session, email="addr3@example.com")
        service = _service(tx_session, redis_client)
        address = await service.create_address(user.id, _address_payload())

        # Act
        updated = await service.update_address(
            user.id, address.id, AddressUpdate(city="Balti")
        )

        # Assert
        assert updated.city == "Balti", "non-default field must be applied"

    async def test_update_address_not_owned_raises_404(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        user = await create_user(tx_session, email="addr4@example.com")
        service = _service(tx_session, redis_client)

        # Act / Assert
        with pytest.raises(Exception) as exc_info:
            await service.update_address(
                user.id, _MISSING_ADDRESS_ID, AddressUpdate(city="X")
            )
        assert exc_info.value.status_code == 404, "missing address must be 404"

    async def test_delete_address_removes_row(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        user = await create_user(tx_session, email="addr5@example.com")
        owned = await persist(
            tx_session,
            Address(
                user_id=user.id,
                full_name="Del Me",
                phone="+37360000010",
                city="Chisinau",
                street="Test 2",
            ),
        )
        service = _service(tx_session, redis_client)

        # Act
        await service.delete_address(user.id, owned.id)

        # Assert
        stmt = select(Address).where(Address.id == owned.id)
        assert (await tx_session.execute(stmt)).scalar_one_or_none() is None, (
            "deleted address must be gone"
        )

    async def test_delete_address_not_owned_raises_404(
        self, tx_session: AsyncSession, redis_client: Redis
    ) -> None:
        # Arrange
        user = await create_user(tx_session, email="addr6@example.com")
        service = _service(tx_session, redis_client)

        # Act / Assert
        with pytest.raises(Exception) as exc_info:
            await service.delete_address(user.id, _MISSING_ADDRESS_ID)
        assert exc_info.value.status_code == 404, "missing address must be 404"
