"""Unit tests for :class:`PromoService.validate_and_compute` (feature A1, §2.5).

Drives the service directly against the commit-safe ``db_session`` so the
validation rules and discount arithmetic are pinned independently of the HTTP
layer: valid percent / fixed, expired window, unknown / inactive code, min-order,
usage limit, and the never-negative clamp.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.promo_service import (
    PromoExpiredError,
    PromoInvalidError,
    PromoMinOrderError,
    PromoService,
    PromoUsageLimitError,
)

pytestmark = pytest.mark.asyncio


async def test_percent_discount(db_session, add_promo) -> None:
    """Percent code: discount = subtotal * value / 100 (whole MDL)."""
    await add_promo("P10", discount_type="percent", discount_value=Decimal("10"))
    service = PromoService(db_session)

    promo, discount = await service.validate_and_compute("P10", Decimal("250.00"))

    assert promo.code == "P10"
    assert discount == Decimal("25")


async def test_percent_discount_rounds_half_up(db_session, add_promo) -> None:
    """Percent rounding is half-up to whole MDL (BUG-02: display == charged).

    10% of 249 is 24.90 raw; the store prices in whole MDL, so the discount is
    quantized to a whole number (25), keeping the shown discount equal to the
    charged one instead of storing 24.90 while the front rounds it to 25.
    """
    await add_promo("P10", discount_type="percent", discount_value=Decimal("10"))
    service = PromoService(db_session)

    _promo, discount = await service.validate_and_compute("P10", Decimal("249.00"))

    # 249 * 10 / 100 = 24.90 → 25 (whole MDL, half-up)
    assert discount == Decimal("25")


async def test_percent_discount_rounds_half_up_down(db_session, add_promo) -> None:
    """A raw discount below the half-point rounds down to whole MDL."""
    await add_promo("P10", discount_type="percent", discount_value=Decimal("10"))
    service = PromoService(db_session)

    _promo, discount = await service.validate_and_compute("P10", Decimal("242.00"))

    # 242 * 10 / 100 = 24.20 → 24 (whole MDL, half-up)
    assert discount == Decimal("24")


async def test_fixed_discount(db_session, add_promo) -> None:
    """Fixed code: discount = flat value (clamped to subtotal)."""
    await add_promo("F30", discount_type="fixed", discount_value=Decimal("30"))
    service = PromoService(db_session)

    _promo, discount = await service.validate_and_compute("F30", Decimal("100.00"))

    assert discount == Decimal("30.00")


async def test_fixed_discount_clamped(db_session, add_promo) -> None:
    """A fixed discount larger than the subtotal is clamped to it (never < 0)."""
    await add_promo("F500", discount_type="fixed", discount_value=Decimal("500"))
    service = PromoService(db_session)

    _promo, discount = await service.validate_and_compute("F500", Decimal("40.00"))

    assert discount == Decimal("40.00")


async def test_unknown_code_raises(db_session) -> None:
    """An unknown code raises ``PromoInvalidError``."""
    service = PromoService(db_session)
    with pytest.raises(PromoInvalidError):
        await service.validate_and_compute("GHOST", Decimal("100.00"))


async def test_inactive_code_raises(db_session, add_promo) -> None:
    """A disabled code raises ``PromoInvalidError``."""
    await add_promo("OFF", is_active=False)
    service = PromoService(db_session)
    with pytest.raises(PromoInvalidError):
        await service.validate_and_compute("OFF", Decimal("100.00"))


async def test_expired_code_raises(db_session, add_promo) -> None:
    """A code outside its window raises ``PromoExpiredError``."""
    now = datetime.now(UTC)
    await add_promo(
        "PAST",
        active_from=now - timedelta(days=10),
        active_to=now - timedelta(days=1),
    )
    service = PromoService(db_session)
    with pytest.raises(PromoExpiredError):
        await service.validate_and_compute("PAST", Decimal("100.00"))


async def test_future_code_raises(db_session, add_promo) -> None:
    """A not-yet-active code raises ``PromoExpiredError``."""
    now = datetime.now(UTC)
    await add_promo(
        "SOON",
        active_from=now + timedelta(days=1),
        active_to=now + timedelta(days=10),
    )
    service = PromoService(db_session)
    with pytest.raises(PromoExpiredError):
        await service.validate_and_compute("SOON", Decimal("100.00"))


async def test_min_order_not_met_raises(db_session, add_promo) -> None:
    """A subtotal below min-order raises ``PromoMinOrderError``."""
    await add_promo("MIN100", min_order_total=Decimal("100"))
    service = PromoService(db_session)
    with pytest.raises(PromoMinOrderError):
        await service.validate_and_compute("MIN100", Decimal("99.99"))


async def test_min_order_met_ok(db_session, add_promo) -> None:
    """A subtotal exactly at min-order is accepted."""
    await add_promo(
        "MIN100",
        discount_type="fixed",
        discount_value=Decimal("10"),
        min_order_total=Decimal("100"),
    )
    service = PromoService(db_session)

    _promo, discount = await service.validate_and_compute("MIN100", Decimal("100.00"))

    assert discount == Decimal("10.00")


async def test_usage_limit_not_reached_ok(db_session, add_promo) -> None:
    """With no prior redemptions, a usage-limited code still applies."""
    await add_promo(
        "LIMIT1",
        discount_type="fixed",
        discount_value=Decimal("5"),
        usage_limit=1,
    )
    service = PromoService(db_session)

    _promo, discount = await service.validate_and_compute("LIMIT1", Decimal("50.00"))

    assert discount == Decimal("5.00")


async def test_usage_limit_reached_raises(db_session, add_promo) -> None:
    """When redemptions reach the limit, the code raises ``PromoUsageLimitError``."""
    from app.models.order import Order

    await add_promo("LIMIT1", usage_limit=1)
    # Simulate one prior redemption by inserting an order snapshotting the code.
    db_session.add(
        Order(
            number="20260101-000001",
            email="x@example.com",
            phone="+3730",
            subtotal=Decimal("100"),
            discount_total=Decimal("10"),
            delivery_cost=Decimal("0"),
            total=Decimal("90"),
            delivery_type="pickup",
            promo_code="LIMIT1",
        )
    )
    await db_session.flush()
    service = PromoService(db_session)

    with pytest.raises(PromoUsageLimitError):
        await service.validate_and_compute("LIMIT1", Decimal("100.00"))
