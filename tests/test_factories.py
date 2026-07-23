"""Smoke tests for the shared factories + async persistence helpers.

Verifies that every composite ``create_*`` helper persists a valid graph
against the real transactional ``db_session`` (FKs, constraints, defaults).
"""

import pytest
from sqlalchemy import func, select

from app.core.security import verify_password
from app.models.cart import CartItem
from app.models.catalog import CategoryTranslation, ProductTranslation
from app.models.content_page import ContentPageTranslation
from app.models.order import OrderItem
from tests.factories import (
    TEST_PASSWORD,
    CartItemFactory,
    create_cart,
    create_category,
    create_content_page,
    create_order,
    create_product,
    create_restock_subscription,
    create_user,
    persist,
)

pytestmark = pytest.mark.asyncio


async def test_create_user_persists_verifiable_password(db_session):
    """create_user assigns an id and a hash that verifies TEST_PASSWORD."""
    user = await create_user(db_session)

    assert user.id is not None
    assert verify_password(TEST_PASSWORD, user.password_hash) is True


async def test_create_category_creates_both_translations(db_session):
    """create_category persists ru+ro translations and a self-path."""
    category = await create_category(db_session)

    count = await db_session.scalar(
        select(func.count())
        .select_from(CategoryTranslation)
        .where(CategoryTranslation.category_id == category.id)
    )
    assert count == 2
    assert category.path == [category.id]


async def test_create_product_creates_translations_and_category(db_session):
    """create_product persists a product with a category and ru+ro rows."""
    product = await create_product(db_session)

    assert product.id is not None
    assert product.category_id is not None
    count = await db_session.scalar(
        select(func.count())
        .select_from(ProductTranslation)
        .where(ProductTranslation.product_id == product.id)
    )
    assert count == 2


async def test_create_cart_with_item(db_session):
    """A cart can hold a persisted line item referencing a product."""
    product = await create_product(db_session)
    cart = await create_cart(db_session, guest=True)
    await persist(
        db_session, CartItemFactory(cart_id=cart.id, product_id=product.id, qty=2)
    )

    qty = await db_session.scalar(
        select(CartItem.qty).where(CartItem.cart_id == cart.id)
    )
    assert qty == 2


async def test_create_order_snapshots_line(db_session):
    """create_order persists an order plus a snapshot order-item line."""
    order = await create_order(db_session, items=1)

    count = await db_session.scalar(
        select(func.count())
        .select_from(OrderItem)
        .where(OrderItem.order_id == order.id)
    )
    assert count == 1
    assert order.number.startswith("TEST-")


async def test_create_restock_subscription_links_user_and_product(db_session):
    """create_restock_subscription links a user to a product, active by default."""
    user = await create_user(db_session)
    product = await create_product(db_session, qty=0)
    sub = await create_restock_subscription(db_session, product=product, user=user)

    assert sub.id is not None
    assert sub.status == "active"
    assert sub.product_id == product.id
    assert sub.user_id == user.id


async def test_create_content_page_creates_translations(db_session):
    """create_content_page persists a page with ru+ro translations."""
    page = await create_content_page(db_session)

    assert page.id is not None
    count = await db_session.scalar(
        select(func.count())
        .select_from(ContentPageTranslation)
        .where(ContentPageTranslation.page_id == page.id)
    )
    assert count == 2
