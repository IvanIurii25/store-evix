"""End-to-end guest journey across the whole storefront surface (B9).

One test walks the full happy path a real anonymous buyer takes, exercising the
catalog read-model, cart, checkout and order-lookup endpoints together through
the *real* app (``async_client`` from the shared harness, ``get_session`` bound to
the rolled-back test session):

    seed an active bilingual product (models + CatalogService)
      -> GET category listing            (read-model / keyset)
      -> GET product detail (PDP)
      -> POST /cart/items  (guest, mints session_token cookie)
      -> GET  /cart        (live-priced)
      -> POST /checkout/quote   (COD, pickup)
      -> POST /checkout         (creates the order atomically)
      -> POST /orders/{number}/lookup  (guest lookup, email in body)

Assertions cover the order-number format (``YYYYMMDD-NNNNNN``), the price/name
snapshot on the order line, and the initial COD status pair
(``new`` / ``pending``).

Run with a dedicated DB::

    EVIX_TEST_DB=evix_test_e2e uv run pytest tests/e2e -q
"""

import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import (
    Category,
    CategoryTranslation,
    Media,
    Product,
    ProductTranslation,
)
from app.services.catalog_service import CatalogService

_API: str = "/api/v1"
_LANG: str = "ro"
_PRICE: Decimal = Decimal("1499.00")
_QTY_IN_STOCK: int = 25
_QTY_ORDERED: int = 2
# YYYYMMDD-NNNNNN — date prefix + zero-padded sequence (§9.7).
_NUMBER_RE = re.compile(r"^\d{8}-\d{6}$")


async def _seed_active_product(session: AsyncSession) -> tuple[str, str, str]:
    """Create one active category + bilingual product and rebuild its card.

    Args:
        session: The rolled-back test session (shared with the app override).

    Returns:
        tuple[str, str, str]: ``(category_slug, product_slug, product_name)`` in
            the ``ro`` language used by the journey.
    """
    category = Category(path=[], depth=0, is_active=True, position=0)
    session.add(category)
    await session.flush()
    category.path = [category.id]
    session.add_all(
        [
            CategoryTranslation(
                category_id=category.id, lang="ru", name="Э2Е", slug="e2e-ru"
            ),
            CategoryTranslation(
                category_id=category.id, lang="ro", name="E2E", slug="e2e-ro"
            ),
        ]
    )

    product = Product(
        category_id=category.id,
        code="E2E-001",
        price=_PRICE,
        qty=_QTY_IN_STOCK,
        is_active=True,
    )
    session.add(product)
    await session.flush()

    ro_name = "Produs E2E"
    session.add_all(
        [
            ProductTranslation(
                product_id=product.id,
                lang="ru",
                name="Товар Э2Е",
                slug="tovar-e2e",
                description="Описание.",
            ),
            ProductTranslation(
                product_id=product.id,
                lang="ro",
                name=ro_name,
                slug="produs-e2e",
                description="Descriere.",
            ),
        ]
    )
    session.add(
        Media(
            product_id=product.id,
            url="https://cdn.example/e2e.jpg",
            kind="image",
            position=0,
        )
    )
    await session.flush()

    # Publish into the denormalized read-model the listing/PDP read from (§5.2).
    await CatalogService(session).rebuild_card(product.id)
    await session.flush()
    return "e2e-ro", "produs-e2e", ro_name


@pytest.mark.asyncio
async def test_guest_journey_catalog_to_order(
    async_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Full guest path: browse -> cart -> checkout -> order lookup (COD).

    Args:
        async_client: The real app's client (from the shared harness).
        db_session: The rolled-back session the app override reads from.
    """
    category_slug, product_slug, product_name = await _seed_active_product(db_session)

    # --- Browse: category listing (read-model, keyset) ---
    listing = await async_client.get(
        f"{_API}/catalog/categories/{category_slug}/products", params={"lang": _LANG}
    )
    assert listing.status_code == 200, listing.text
    cards = listing.json()["data"]
    assert len(cards) == 1
    card = cards[0]
    assert card["slug"] == product_slug
    assert card["name"] == product_name
    assert card["in_stock"] is True
    product_id = card["product_id"]

    # --- Browse: PDP ---
    pdp = await async_client.get(
        f"{_API}/catalog/products/{product_slug}", params={"lang": _LANG}
    )
    assert pdp.status_code == 200, pdp.text
    detail = pdp.json()
    assert detail["code"] == "E2E-001"
    assert detail["id"] == product_id
    # Both language slugs are exposed for the language switcher (§2.1.1).
    assert {s["lang"] for s in detail["slugs"]} == {"ru", "ro"}

    # --- Cart: add as a guest (mints the session_token cookie) ---
    add = await async_client.post(
        f"{_API}/cart/items", json={"product_id": product_id, "qty": _QTY_ORDERED}
    )
    assert add.status_code == 201, add.text
    assert "session_token" in async_client.cookies  # guest identity established

    cart = await async_client.get(f"{_API}/cart")
    assert cart.status_code == 200, cart.text
    cart_body = cart.json()
    assert cart_body["item_count"] == _QTY_ORDERED
    expected_subtotal = _PRICE * _QTY_ORDERED
    assert Decimal(cart_body["subtotal"]) == expected_subtotal

    # --- Checkout: quote (idempotent predraft, pickup => free delivery) ---
    quote = await async_client.post(
        f"{_API}/checkout/quote", json={"delivery_type": "pickup"}
    )
    assert quote.status_code == 200, quote.text
    quote_body = quote.json()
    assert Decimal(quote_body["subtotal"]) == expected_subtotal
    assert Decimal(quote_body["delivery_cost"]) == Decimal("0")
    assert Decimal(quote_body["total"]) == expected_subtotal
    assert quote_body["item_count"] == _QTY_ORDERED

    # --- Checkout: create the COD order atomically ---
    buyer_email = "guest@example.com"
    checkout = await async_client.post(
        f"{_API}/checkout",
        json={
            "email": buyer_email,
            "phone": "+37360000000",
            "delivery_type": "pickup",
        },
    )
    assert checkout.status_code == 201, checkout.text
    order = checkout.json()
    number = order["number"]

    # Number format YYYYMMDD-NNNNNN with today's UTC date prefix (§9.7).
    assert _NUMBER_RE.match(number), number
    assert number.startswith(datetime.now(UTC).strftime("%Y%m%d"))

    # COD lands as new / pending (§8).
    assert order["status"] == "new"
    assert order["payment_status"] == "pending"
    assert order["payment_method"] == "cod"
    assert Decimal(order["total"]) == expected_subtotal

    # Snapshotted line: name + price frozen at order time (§2.4).
    assert len(order["items"]) == 1
    line = order["items"][0]
    assert line["product_id"] == product_id
    assert line["name_snapshot"] == product_name
    assert Decimal(line["price_snapshot"]) == _PRICE
    assert line["qty"] == _QTY_ORDERED

    # --- Order lookup: guest by number + email (email in body, not URL) ---
    lookup = await async_client.post(
        f"{_API}/orders/{number}/lookup", json={"email": buyer_email}
    )
    assert lookup.status_code == 200, lookup.text
    assert lookup.json()["number"] == number

    # Wrong email must not leak the order's existence (§9).
    denied = await async_client.post(
        f"{_API}/orders/{number}/lookup", json={"email": "someone-else@example.com"}
    )
    assert denied.status_code == 404
