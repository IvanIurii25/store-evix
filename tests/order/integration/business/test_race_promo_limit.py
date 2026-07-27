"""Concurrency: two checkouts race to redeem a usage_limit=1 promo (§2.5).

The usage counter is derived (``count_orders_using`` counts orders carrying the
code), so two concurrent checkouts would both read a pre-insert count of 0, both
pass the limit check, and both redeem — overshooting ``usage_limit``. The fix
locks the promo row ``FOR UPDATE`` before counting (checkout only), turning it
into a per-code mutex: the second redeemer blocks until the first commits, then
counts the now-visible order and is rejected with ``PromoUsageLimitError``.

Opts out of SAVEPOINT isolation like the other races: two independent engines run
two real committing checkouts against the same code. ``real_engine`` truncates
the touched tables (incl. ``promo_code``) afterwards.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.cart import Cart, CartItem
from app.models.catalog import Category, Product, ProductTranslation
from app.models.order import Order
from app.models.promo import PromoCode
from app.services.checkout_service import CheckoutService
from app.services.promo_service import PromoUsageLimitError

pytestmark = pytest.mark.asyncio

_CODE = "ONCE"


async def _seed(factory: async_sessionmaker) -> tuple:
    """Commit a usage_limit=1 promo, an in-stock product, and two guest carts."""
    now = datetime.now(UTC)
    token_a, token_b = uuid4(), uuid4()
    async with factory() as session:
        session.add(Category(id=1, parent_id=None, path=[1], depth=0, is_active=True))
        session.add(
            Product(
                id=1,
                category_id=1,
                code="P1",
                price=Decimal("100.00"),
                qty=5,  # ample stock — the promo limit, not stock, is the gate
                is_active=True,
            )
        )
        session.add(
            ProductTranslation(product_id=1, lang="ro", name="Prod", slug="prod")
        )
        session.add(
            PromoCode(
                id=1,
                code=_CODE,
                discount_type="fixed",
                discount_value=Decimal("10.00"),
                active_from=now - timedelta(days=1),
                active_to=now + timedelta(days=1),
                usage_limit=1,
                is_active=True,
            )
        )
        for token in (token_a, token_b):
            cart = Cart(user_id=None, session_token=token, status="draft")
            session.add(cart)
            await session.flush()
            session.add(CartItem(cart_id=cart.id, product_id=1, qty=1))
        await session.commit()
    return token_a, token_b


async def _checkout(factory: async_sessionmaker, token):
    """Run one guest COD checkout redeeming the promo; return result or exception."""
    async with factory() as session:
        service = CheckoutService(session)
        return await service.checkout(
            user_id=None,
            session_token=token,
            email="race@example.com",
            phone="+37360000010",
            delivery_type="pickup",
            delivery_address_id=None,
            promo_code=_CODE,
        )


async def test_two_concurrent_redemptions_honor_usage_limit(real_engine) -> None:
    """A usage_limit=1 code redeemed once: one checkout wins, one is rejected."""
    _, factory_seed = real_engine()
    token_a, token_b = await _seed(factory_seed)

    _, factory_a = real_engine()
    _, factory_b = real_engine()

    results = await asyncio.gather(
        _checkout(factory_a, token_a),
        _checkout(factory_b, token_b),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, Exception)]
    rejections = [r for r in results if isinstance(r, PromoUsageLimitError)]
    assert len(successes) == 1, f"expected one redemption, got results={results}"
    assert len(rejections) == 1, f"expected one rejection, got results={results}"

    # Exactly one order carries the code — the limit held.
    async with factory_seed() as session:
        redeemed = (
            await session.execute(
                select(func.count())
                .select_from(Order)
                .where(Order.promo_code == _CODE)
            )
        ).scalar_one()
        assert redeemed == 1
