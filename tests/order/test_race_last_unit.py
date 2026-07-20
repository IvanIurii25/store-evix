"""Concurrency: two checkouts race for the last unit in stock (B6 DoD).

Contract (§9): stock is decremented with ``UPDATE ... WHERE qty >= :n`` so the
decrement is race-safe at the SQL level (not read-modify-write). With one unit on
hand and two concurrent ``POST /checkout`` calls, exactly one must succeed (201)
and one must be rejected (409 ``out_of_stock``); the product ends at ``qty = 0``.

This test deliberately opts out of the suite's SAVEPOINT rollback isolation: a
real commit race cannot be reproduced inside a single rolled-back transaction.
Two *independent* engines each run their own committing session bound to a
distinct app instance, and both target the same guest cart (same
``session_token``) holding the single unit. The ``real_engine`` fixture truncates
the order/cart/product tables afterwards, so no residue leaks into other tests.
"""

import asyncio
from collections.abc import AsyncGenerator
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.routers.cart import router as cart_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.models.cart import Cart, CartItem
from app.models.catalog import Category, Product, ProductTranslation
from tests.order.conftest import _import_order_routers

pytestmark = pytest.mark.asyncio

_API_V1_PREFIX = "/api/v1"


def _build_committing_app(factory: async_sessionmaker) -> FastAPI:
    """Assemble an app whose ``get_session`` yields a real committing session.

    Args:
        factory: A sessionmaker bound to an independent real engine.

    Returns:
        FastAPI: The configured application (cart + checkout + orders).
    """
    checkout_router, orders_router = _import_order_routers()
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(cart_router, prefix=_API_V1_PREFIX)
    app.include_router(checkout_router, prefix=_API_V1_PREFIX)
    app.include_router(orders_router, prefix=_API_V1_PREFIX)

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    return app


async def _seed_last_unit(factory: async_sessionmaker):
    """Commit one product (qty=1) plus a guest cart holding that single unit.

    Args:
        factory: Sessionmaker on a real engine (committed, visible to others).

    Returns:
        uuid.UUID: The guest cart's ``session_token`` (checkout finds it by this).
    """
    token = uuid4()
    async with factory() as session:
        session.add(Category(id=1, parent_id=None, path=[1], depth=0, is_active=True))
        session.add(
            Product(
                id=1,
                category_id=1,
                code="LAST",
                price=Decimal("100.00"),
                qty=1,
                is_active=True,
            )
        )
        session.add(
            ProductTranslation(product_id=1, lang="ro", name="LastUnit", slug="last")
        )
        cart = Cart(user_id=None, session_token=token, status="draft")
        session.add(cart)
        await session.flush()
        session.add(CartItem(cart_id=cart.id, product_id=1, qty=1))
        await session.commit()
    return token


async def _checkout(app: FastAPI, token) -> int:
    """Run one guest checkout with the given cart cookie; return the status code."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        http.cookies.set("session_token", str(token))
        resp = await http.post(
            "/api/v1/checkout",
            json={
                "email": "race@example.com",
                "phone": "+37360000010",
                "delivery_type": "pickup",
            },
        )
        return resp.status_code


async def test_two_concurrent_checkouts_last_unit(real_engine) -> None:
    """Exactly one of two concurrent last-unit checkouts wins; stock ends at 0."""
    _, factory_seed = real_engine()
    token = await _seed_last_unit(factory_seed)

    # Two fully independent engines → two real concurrent transactions.
    _, factory_a = real_engine()
    _, factory_b = real_engine()
    app_a = _build_committing_app(factory_a)
    app_b = _build_committing_app(factory_b)

    status_a, status_b = await asyncio.gather(
        _checkout(app_a, token),
        _checkout(app_b, token),
    )

    statuses = sorted([status_a, status_b])
    assert statuses == [201, 409], f"expected one 201 + one 409, got {statuses}"

    # Final stock must be exactly zero — the winning order took the last unit.
    async with factory_seed() as session:
        product = (
            await session.execute(select(Product).where(Product.id == 1))
        ).scalar_one()
        assert product.qty == 0
