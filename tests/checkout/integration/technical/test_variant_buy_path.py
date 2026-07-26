"""P2b–d: variant threading through cart → quick-buy → variant stock authority."""

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import (
    Attribute,
    AttributeValue,
    AttributeValueTranslation,
    ProductVariant,
    ProductVariantValue,
    ProductVariationAttribute,
)
from app.services.cart_service import CartService, ProductNotAvailableError
from app.services.checkout_service import (
    CheckoutService,
    OutOfStockError,
    ProductNotFoundError,
)

# Fixed guest token so the cart persists across service calls within a test.
_TOKEN = UUID("00000000-0000-0000-0000-0000000006aa")


async def _make_variable(
    session: AsyncSession, add_product, *, product_id: int, prices, qtys
) -> dict[str, int]:
    """Variable product (color: Bej/Gri) with two variants; returns key ids."""
    product = await add_product(
        product_id, price=Decimal(prices[0]), code=f"VP-{product_id}", qty=0
    )
    product.has_variants = True

    attr = Attribute(code=f"color{product_id}")
    session.add(attr)
    await session.flush()
    session.add(
        ProductVariationAttribute(
            product_id=product_id, attribute_id=attr.id, position=0
        )
    )

    ids: dict[str, int] = {}
    for idx, (key, ro, price, qty) in enumerate(
        (("bej", "Bej", prices[0], qtys[0]), ("gri", "Gri", prices[1], qtys[1]))
    ):
        value = AttributeValue(attribute_id=attr.id)
        session.add(value)
        await session.flush()
        session.add(
            AttributeValueTranslation(value_id=value.id, lang="ro", value=ro)
        )
        variant = ProductVariant(
            product_id=product_id,
            code=f"VP-{product_id}-{key}",
            price=Decimal(price),
            qty=qty,
            position=idx,
            is_active=True,
        )
        session.add(variant)
        await session.flush()
        session.add(ProductVariantValue(variant_id=variant.id, value_id=value.id))
        ids[f"value_{key}"] = value.id
        ids[f"variant_{key}"] = variant.id
    await session.flush()
    return ids


@pytest.mark.asyncio
async def test_cart_add_requires_and_prices_variant(db_session, add_product):
    """Variable product: add needs a valid variant; cart shows its price + label."""
    ids = await _make_variable(
        db_session, add_product, product_id=600, prices=("179.00", "249.00"), qtys=(5, 5)
    )
    service = CartService(db_session)

    with pytest.raises(ProductNotAvailableError):  # missing variant
        await service.add_item(None, _TOKEN, 600, 1, "ro", None)
    with pytest.raises(ProductNotAvailableError):  # unknown variant
        await service.add_item(None, _TOKEN, 600, 1, "ro", 999999)

    cart = await service.add_item(None, _TOKEN, 600, 2, "ro", ids["variant_gri"])
    assert len(cart.items) == 1
    line = cart.items[0]
    assert line.variant_id == ids["variant_gri"]
    assert line.price == Decimal("249.00")  # variant price, not product base
    assert line.variant_label == "Gri"
    assert line.line_total == Decimal("498.00")


@pytest.mark.asyncio
async def test_cart_add_simple_forbids_variant(db_session, add_product):
    """A simple product rejects a variant_id."""
    await add_product(601, price=Decimal("50.00"), code="SIMPLE-601", qty=5)
    service = CartService(db_session)
    with pytest.raises(ProductNotAvailableError):
        await service.add_item(None, _TOKEN, 601, 1, "ro", 12345)


@pytest.mark.asyncio
async def test_quick_buy_decrements_variant_stock_not_product(db_session, add_product):
    """quick_buy on a variant decrements that variant's qty and snapshots options."""
    ids = await _make_variable(
        db_session, add_product, product_id=602, prices=("179.00", "249.00"), qtys=(4, 9)
    )
    service = CheckoutService(db_session)

    order = await service.quick_buy(
        product_id=602, variant_id=ids["variant_gri"], phone="069000000", qty=3
    )
    assert order.items[0].price_snapshot == Decimal("249.00")
    assert "Gri" in order.items[0].name_snapshot

    gri = await db_session.get(ProductVariant, ids["variant_gri"])
    bej = await db_session.get(ProductVariant, ids["variant_bej"])
    assert gri.qty == 6  # 9 - 3, variant is the stock authority
    assert bej.qty == 4  # untouched


@pytest.mark.asyncio
async def test_quick_buy_variant_oversell_rejected(db_session, add_product):
    """Buying more than a variant's stock raises OutOfStock (no order)."""
    ids = await _make_variable(
        db_session, add_product, product_id=603, prices=("10.00", "10.00"), qtys=(2, 0)
    )
    service = CheckoutService(db_session)
    with pytest.raises(OutOfStockError):
        await service.quick_buy(
            product_id=603, variant_id=ids["variant_gri"], phone="069000000", qty=1
        )


@pytest.mark.asyncio
async def test_quick_buy_variable_without_variant_rejected(db_session, add_product):
    """Variable product quick-buy without a variant is a not-found."""
    await _make_variable(
        db_session, add_product, product_id=604, prices=("10.00", "10.00"), qtys=(2, 2)
    )
    service = CheckoutService(db_session)
    with pytest.raises(ProductNotFoundError):
        await service.quick_buy(product_id=604, phone="069000000", qty=1)
