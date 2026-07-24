"""Right-to-erasure tests (LP195/2024 Art.17) — account anonymization.

Covers the destructive path directly: the service scrubs the profile and deletes
addresses / restock subscriptions / carts while KEEPING orders (fiscal), and the
``DELETE /users/me`` endpoint returns 204, clears the auth cookies and leaves the
account unable to authenticate.
"""

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_password
from app.models.cart import Cart
from app.models.order import Order
from app.models.restock import RestockSubscription
from app.models.user import Address, AppUser
from app.services.auth_service import AuthService
from tests.factories import (
    TEST_PASSWORD,
    AddressFactory,
    CartItemFactory,
    create_cart,
    create_order,
    create_product,
    create_restock_subscription,
    create_user,
    persist,
)

pytestmark = pytest.mark.asyncio

_ERASE_PW = "Str0ng-Pass-123"


async def _count(session: AsyncSession, model, user_id: int) -> int:
    return await session.scalar(
        select(func.count()).select_from(model).where(model.user_id == user_id)
    )


class TestAccountErasure:
    async def test_anonymize_scrubs_profile_deletes_pii_keeps_orders(
        self, tx_session: AsyncSession, redis_client: Redis
    ):
        """Erasure scrubs the profile + drops addresses/subs/carts, keeps orders."""
        # Arrange: a user with every kind of attached personal data + an order.
        user = await create_user(
            tx_session, email="real@example.com", phone="+37360000123"
        )
        await persist(tx_session, AddressFactory(user_id=user.id))
        product = await create_product(tx_session, qty=0)
        await create_restock_subscription(tx_session, product=product, user=user)
        cart = await create_cart(tx_session, user=user)
        await persist(
            tx_session, CartItemFactory(cart_id=cart.id, product_id=product.id)
        )
        order = await create_order(tx_session, user=user, items=1)
        uid, order_id = user.id, order.id

        # Act
        await AuthService(session=tx_session, redis=redis_client).anonymize_account(uid)

        # Assert: profile anonymized + deactivated.
        erased = await tx_session.get(AppUser, uid)
        assert erased.email == f"deleted-{uid}@anonymized.local"
        assert erased.phone is None
        assert erased.is_active is False
        assert erased.is_staff is False
        assert erased.role is None
        # Non-essential personal data deleted.
        assert await _count(tx_session, Address, uid) == 0
        assert await _count(tx_session, RestockSubscription, uid) == 0
        assert await _count(tx_session, Cart, uid) == 0
        # Order kept (lawful fiscal obligation, Art.17(3)(b)).
        assert await tx_session.get(Order, order_id) is not None

    async def test_anonymize_replaces_password_so_it_cannot_verify(
        self, tx_session: AsyncSession, redis_client: Redis
    ):
        """The known test password no longer verifies after erasure."""
        user = await create_user(tx_session)  # UserFactory → TEST_PASSWORD hash
        old_hash = user.password_hash

        await AuthService(session=tx_session, redis=redis_client).anonymize_account(
            user.id
        )

        erased = await tx_session.get(AppUser, user.id)
        assert erased.password_hash != old_hash
        assert verify_password(TEST_PASSWORD, erased.password_hash) is False

    async def test_delete_me_endpoint_204_then_unauthenticated(
        self, client: AsyncClient
    ):
        """DELETE /users/me erases the account; the session no longer authenticates."""
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": "erase-ep@example.com",
                "phone": "+37360000777",
                "password": _ERASE_PW,
            },
        )
        login = await client.post(
            "/api/v1/auth/login",
            json={"email": "erase-ep@example.com", "password": _ERASE_PW},
        )
        assert login.status_code == 200
        # The Secure login cookie isn't sent over http-test; use the bearer token.
        headers = {"Authorization": f"Bearer {login.json()['access']}"}

        me = await client.get("/api/v1/users/me", headers=headers)
        assert me.status_code == 200

        resp = await client.delete("/api/v1/users/me", headers=headers)
        assert resp.status_code == 204

        # The account is now inactive, so even a still-valid token no longer authenticates.
        after = await client.get("/api/v1/users/me", headers=headers)
        assert after.status_code == 401
