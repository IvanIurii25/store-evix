"""Concurrency: cart merge races an add_item on the same user cart (§7).

Merge sums a guest line into the user line with an in-memory ``qty +=`` (a
read-modify-write). If a concurrent ``add_item`` on the same user cart runs in
between, one increment is lost. The fix locks the user cart row ``FOR UPDATE`` on
every write path (add/update/remove/merge), so mutations of one cart serialize on
its ``cart`` row: the two increments both land.

Opts out of SAVEPOINT isolation like the other races: two independent engines run
a real merge and a real add concurrently. ``real_engine`` truncates the touched
tables (incl. ``app_user``) afterwards.
"""

import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.cart import Cart, CartItem
from app.models.catalog import Category, Product, ProductTranslation
from app.models.user import AppUser
from app.services.cart_service import CartService

pytestmark = pytest.mark.asyncio

_USER_ID = 1
_PRODUCT_ID = 1
_USER_QTY = 2  # user cart already holds 2 of the product
_GUEST_QTY = 3  # guest cart holds 3 (merge should add these)
_ADD_QTY = 5  # concurrent add_item adds 5 more


async def _seed(factory: async_sessionmaker):
    """Commit a user with a stocked cart line plus a guest cart to merge in."""
    token = uuid4()
    async with factory() as session:
        session.add(AppUser(id=_USER_ID, email="u@e.md", password_hash="x"))
        session.add(Category(id=1, parent_id=None, path=[1], depth=0, is_active=True))
        session.add(
            Product(
                id=_PRODUCT_ID,
                category_id=1,
                code="P1",
                price=Decimal("100.00"),
                qty=100,
                is_active=True,
            )
        )
        session.add(
            ProductTranslation(product_id=_PRODUCT_ID, lang="ro", name="P", slug="p")
        )
        await session.flush()  # user/product must exist before their FKs below
        user_cart = Cart(user_id=_USER_ID, session_token=None, status="draft")
        guest_cart = Cart(user_id=None, session_token=token, status="draft")
        session.add_all([user_cart, guest_cart])
        await session.flush()
        session.add(
            CartItem(cart_id=user_cart.id, product_id=_PRODUCT_ID, qty=_USER_QTY)
        )
        session.add(
            CartItem(cart_id=guest_cart.id, product_id=_PRODUCT_ID, qty=_GUEST_QTY)
        )
        await session.commit()
    return token


async def _merge(factory: async_sessionmaker, token) -> None:
    async with factory() as session:
        await CartService(session).merge_guest_into_user(_USER_ID, token)


async def _add(factory: async_sessionmaker) -> None:
    async with factory() as session:
        await CartService(session).add_item(
            _USER_ID, None, product_id=_PRODUCT_ID, qty=_ADD_QTY
        )


async def test_merge_and_add_do_not_lose_updates(real_engine) -> None:
    """Merge (+3) and a concurrent add (+5) on the same line both land (2->10)."""
    _, factory_seed = real_engine()
    token = await _seed(factory_seed)

    _, factory_a = real_engine()
    _, factory_b = real_engine()

    await asyncio.gather(_merge(factory_a, token), _add(factory_b))

    async with factory_seed() as session:
        lines = (
            await session.execute(
                select(CartItem)
                .join(Cart, Cart.id == CartItem.cart_id)
                .where(Cart.user_id == _USER_ID, CartItem.product_id == _PRODUCT_ID)
            )
        ).scalars().all()
        # Exactly one merged line, holding every increment: 2 + 3 + 5.
        assert len(lines) == 1, f"expected one user line, got {len(lines)}"
        assert lines[0].qty == _USER_QTY + _GUEST_QTY + _ADD_QTY
