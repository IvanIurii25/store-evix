"""API tests for the dashboard + traffic analytics + tracking (§6.3).

Covers: the business summary (revenue excl. canceled, order counts, AOV, status
split, low-stock queue, best sellers); the revenue series; pageview ingest via
the public track endpoint (human vs bot classification by User-Agent); the
traffic summary + series; and the ``current_staff`` guard.
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Category, Product
from app.models.order import Order, OrderItem

pytestmark = pytest.mark.asyncio

# A realistic desktop browser UA (classified as human, device=desktop).
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
# An obvious crawler UA (classified as a bot).
_BOT_UA = "Googlebot/2.1 (+http://www.google.com/bot.html)"


async def _make_category(session: AsyncSession) -> Category:
    """Persist an active root category with a self-referential path."""
    category = Category(parent_id=None, path=[], depth=0, is_active=True, position=0)
    session.add(category)
    await session.flush()
    category.path = [category.id]
    await session.flush()
    return category


async def _make_product(
    session: AsyncSession,
    category_id: int,
    code: str,
    *,
    qty: int,
    price: str = "50.00",
) -> Product:
    """Persist a product in the given category."""
    product = Product(
        category_id=category_id,
        code=code,
        price=Decimal(price),
        qty=qty,
        is_active=True,
    )
    session.add(product)
    await session.flush()
    return product


async def _make_order(
    session: AsyncSession,
    number: str,
    total: str,
    *,
    status: str = "done",
) -> Order:
    """Persist a minimal order."""
    order = Order(
        number=number,
        email="buyer@example.com",
        phone="+37360000000",
        status=status,
        subtotal=Decimal(total),
        total=Decimal(total),
        delivery_type="pickup",
    )
    session.add(order)
    await session.flush()
    return order


# --------------------------------------------------------------------------- #
# Business dashboard
# --------------------------------------------------------------------------- #
async def test_dashboard_summary(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Summary reports revenue (excl. canceled), counts, AOV, low stock, top."""
    category = await _make_category(db_session)
    low = await _make_product(db_session, category.id, "LOW-1", qty=1)
    await _make_product(db_session, category.id, "OK-1", qty=100)

    done = await _make_order(db_session, "ORD-1", "100.00", status="done")
    db_session.add(
        OrderItem(
            order_id=done.id,
            product_id=low.id,
            name_snapshot="Widget",
            price_snapshot=Decimal("50.00"),
            qty=2,
        )
    )
    await _make_order(db_session, "ORD-2", "999.00", status="canceled")
    await db_session.flush()

    resp = await client.get("/api/v1/admin/dashboard/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert Decimal(body["revenue"]) == Decimal("100.00")
    assert body["orders_count"] == 2
    assert body["paid_orders_count"] == 1
    assert Decimal(body["avg_order_value"]) == Decimal("100.00")

    status_counts = {row["name"]: row["count"] for row in body["status_distribution"]}
    assert status_counts == {"done": 1, "canceled": 1}

    assert body["low_stock_count"] >= 1
    low_codes = {item["code"] for item in body["low_stock"]}
    assert "LOW-1" in low_codes and "OK-1" not in low_codes

    top = body["top_products"]
    assert top[0]["name"] == "Widget"
    assert top[0]["qty_sold"] == 2
    assert Decimal(top[0]["revenue"]) == Decimal("100.00")


async def test_revenue_series(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The revenue series has a bucket covering today's order."""
    await _make_order(db_session, "ORD-S1", "40.00", status="done")
    await db_session.flush()

    resp = await client.get("/api/v1/admin/dashboard/revenue-series")
    assert resp.status_code == 200, resp.text
    points = resp.json()["data"]
    assert len(points) >= 1
    assert sum(Decimal(p["revenue"]) for p in points) >= Decimal("40.00")


# --------------------------------------------------------------------------- #
# Traffic analytics + tracking
# --------------------------------------------------------------------------- #
async def _track(
    client: AsyncClient,
    path: str,
    session_id: str,
    ua: str,
    referrer: str | None = None,
) -> None:
    """Post one pageview with the given User-Agent."""
    body: dict = {"path": path, "session_id": session_id}
    if referrer is not None:
        body["referrer"] = referrer
    resp = await client.post(
        "/api/v1/track/pageview",
        json=body,
        headers={"User-Agent": ua},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


async def test_track_and_analytics_summary(client: AsyncClient) -> None:
    """Human pageviews count toward metrics; bots are separated out."""
    await _track(client, "/ro", "sess-A", _BROWSER_UA, referrer="https://google.com")
    await _track(client, "/ro/p/phone", "sess-A", _BROWSER_UA)
    await _track(client, "/ro", "sess-bot", _BOT_UA)

    resp = await client.get("/api/v1/admin/analytics/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["pageviews"] == 2  # two human views (same session)
    assert body["unique_visitors"] == 1
    assert body["bot_pageviews"] == 1

    paths = {row["name"]: row["count"] for row in body["top_paths"]}
    assert paths == {"/ro": 1, "/ro/p/phone": 1}

    referrers = {row["name"]: row["count"] for row in body["top_referrers"]}
    assert referrers == {"https://google.com": 1}

    devices = {row["name"]: row["count"] for row in body["device_breakdown"]}
    assert devices.get("desktop") == 2
    assert devices.get("bot") == 1


async def test_traffic_series(client: AsyncClient) -> None:
    """The traffic series has today's bucket with human pageviews."""
    await _track(client, "/ro", "sess-X", _BROWSER_UA)
    resp = await client.get("/api/v1/admin/analytics/traffic-series")
    assert resp.status_code == 200, resp.text
    points = resp.json()["data"]
    assert len(points) >= 1
    assert sum(p["pageviews"] for p in points) >= 1


# --------------------------------------------------------------------------- #
# Guard
# --------------------------------------------------------------------------- #
async def test_guard_blocks_anonymous(guest_client: AsyncClient) -> None:
    """Without a token the real ``current_staff`` dependency yields 401."""
    resp = await guest_client.get("/api/v1/admin/dashboard/summary")
    assert resp.status_code == 401
