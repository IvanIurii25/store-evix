"""Email-domain test fixtures.

Provides a MailHog gate + purge helper for the SMTP backend test, and a cart
seeder for the checkout-integration test. Reuses the top-level ``db_session``
(commit-safe SAVEPOINT) so the checkout service's ``commit()`` stays isolated.

Run with ``EVIX_TEST_DB=evix_test_email``.
"""

from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cart import Cart, CartItem
from app.models.catalog import Category, Product, ProductTranslation

# MailHog endpoints (SMTP on 51025, HTTP UI/API on 58025).
MAILHOG_API: str = "http://localhost:58025"
_MESSAGES_URL: str = f"{MAILHOG_API}/api/v2/messages"
_PURGE_URL: str = f"{MAILHOG_API}/api/v1/messages"


def _mailhog_available() -> bool:
    """Return whether the MailHog HTTP API answers within a short timeout."""
    try:
        resp = httpx.get(_MESSAGES_URL, timeout=2.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture
def mailhog() -> str:
    """Skip unless MailHog is reachable; purge its inbox and yield its API base."""
    if not _mailhog_available():
        pytest.skip("MailHog is not available on localhost:58025")
    httpx.delete(_PURGE_URL, timeout=2.0)
    return MAILHOG_API


@pytest_asyncio.fixture
async def seed_cart(db_session: AsyncSession):
    """Return a helper seeding a guest cart with one line; yields its token."""

    async def _ensure_category() -> None:
        if await db_session.get(Category, 1) is None:
            db_session.add(
                Category(id=1, parent_id=None, path=[1], depth=0, is_active=True)
            )
            await db_session.flush()

    async def _seed(
        *,
        product_id: int,
        price: Decimal,
        qty: int,
        stock: int = 100,
    ):
        await _ensure_category()
        db_session.add(
            Product(
                id=product_id,
                category_id=1,
                code=f"SKU-{product_id}",
                price=price,
                qty=stock,
                is_active=True,
            )
        )
        db_session.add(
            ProductTranslation(
                product_id=product_id,
                lang="ro",
                name="Product",
                slug=f"slug-{product_id}",
            )
        )
        token = uuid4()
        cart = Cart(session_token=token, status="draft")
        db_session.add(cart)
        await db_session.flush()
        db_session.add(CartItem(cart_id=cart.id, product_id=product_id, qty=qty))
        await db_session.flush()
        return token

    return _seed
