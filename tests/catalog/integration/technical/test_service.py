"""Service-layer tests: breadcrumbs, publication rule, rebuild_card, tree."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.cart import Cart, CartItem
from app.models.catalog import ProductCard
from app.services.catalog_service import (
    CatalogService,
    NotFoundError,
    PublicationError,
)


async def _seed_product(seed, *, product_id: int, slug: str) -> None:
    """Insert a publishable two-lang product for in-cart-count tests."""
    await seed["add_product"](
        seed["session"],
        product_id=product_id,
        category_id=2,
        code=f"P-CART-{product_id}",
        price=Decimal("100.00"),
        qty=5,
        slugs={"ru": f"{slug}-ru", "ro": f"{slug}-ro"},
        names={"ru": "Товар", "ro": "Produs"},
    )
    await seed["session"].flush()


async def _add_cart(seed, *, product_id: int, status: str) -> None:
    """Persist one cart of ``status`` holding ``product_id``."""
    session = seed["session"]
    cart = Cart(status=status)
    session.add(cart)
    await session.flush()
    session.add(CartItem(cart_id=cart.id, product_id=product_id, qty=1))
    await session.flush()


@pytest.mark.asyncio
async def test_in_cart_count_zero_when_no_carts(seed):
    """PDP in_cart_count is 0 when no cart holds the product."""
    await seed["build_tree"]()
    await _seed_product(seed, product_id=400, slug="c-zero")
    service: CatalogService = seed["service"]

    detail = await service.get_product("c-zero-ro", "ro")

    assert detail.in_cart_count == 0


@pytest.mark.asyncio
async def test_in_cart_count_counts_live_carts(seed):
    """in_cart_count reflects the real number of distinct draft carts (1 and N)."""
    await seed["build_tree"]()
    await _seed_product(seed, product_id=401, slug="c-live")
    for _ in range(3):
        await _add_cart(seed, product_id=401, status="draft")
    service: CatalogService = seed["service"]

    detail = await service.get_product("c-live-ro", "ro")

    assert detail.in_cart_count == 3


@pytest.mark.asyncio
async def test_in_cart_count_excludes_converted_and_abandoned(seed):
    """Converted/abandoned carts are not counted — only live (draft) carts."""
    await seed["build_tree"]()
    await _seed_product(seed, product_id=402, slug="c-dead")
    await _add_cart(seed, product_id=402, status="draft")
    await _add_cart(seed, product_id=402, status="converted")
    await _add_cart(seed, product_id=402, status="abandoned")
    service: CatalogService = seed["service"]

    detail = await service.get_product("c-dead-ro", "ro")

    # Only the single draft cart counts.
    assert detail.in_cart_count == 1


@pytest.mark.asyncio
async def test_breadcrumbs_from_path(seed):
    """Breadcrumbs are resolved from category.path in root..self order."""
    await seed["build_tree"]()
    service: CatalogService = seed["service"]

    detail = await service.get_category("telefoane", "ro")

    assert [crumb.id for crumb in detail.breadcrumbs] == [1, 2]
    assert [crumb.slug for crumb in detail.breadcrumbs] == [
        "electronice",
        "telefoane",
    ]
    assert detail.name == "Telefoane"


@pytest.mark.asyncio
async def test_category_tree(seed):
    """The tree nests children under their parent root node."""
    await seed["build_tree"]()
    service: CatalogService = seed["service"]

    roots = await service.list_categories("ru")

    assert len(roots) == 1
    root = roots[0]
    assert root.id == 1
    assert root.slug == "electronika"
    assert len(root.children) == 1
    assert root.children[0].id == 2
    assert root.children[0].slug == "telefony"


@pytest.mark.asyncio
async def test_category_detail_children(seed):
    """Category detail exposes direct active children."""
    await seed["build_tree"]()
    service: CatalogService = seed["service"]

    detail = await service.get_category("electronice", "ro")

    assert detail.id == 1
    assert [child.id for child in detail.children] == [2]


@pytest.mark.asyncio
async def test_category_detail_not_found(seed):
    """Unknown slug raises NotFoundError."""
    await seed["build_tree"]()
    service: CatalogService = seed["service"]

    with pytest.raises(NotFoundError):
        await service.get_category("nope", "ro")


@pytest.mark.asyncio
async def test_publication_rule_single_lang_rejected(seed):
    """Activating a product with only one translation raises PublicationError."""
    await seed["build_tree"]()
    session = seed["session"]
    service: CatalogService = seed["service"]

    # Product active but only ro translation present -> rule violated.
    await seed["add_product"](
        session,
        product_id=10,
        category_id=2,
        code="P-ONE-LANG",
        price=Decimal("100.00"),
        qty=5,
        slugs={"ro": "produs-ro"},
        names={"ro": "Produs"},
        is_active=True,
    )
    await session.flush()

    with pytest.raises(PublicationError):
        await service.assert_publishable(10)


@pytest.mark.asyncio
async def test_publication_rule_both_langs_ok(seed):
    """A product with both translations passes the publication check."""
    await seed["build_tree"]()
    session = seed["session"]
    service: CatalogService = seed["service"]

    await seed["add_product"](
        session,
        product_id=11,
        category_id=2,
        code="P-BOTH",
        price=Decimal("200.00"),
        qty=3,
        slugs={"ru": "telefon-ru", "ro": "telefon-ro"},
        names={"ru": "Телефон", "ro": "Telefon"},
    )
    await session.flush()

    # Does not raise.
    await service.assert_publishable(11)


@pytest.mark.asyncio
async def test_rebuild_card_creates_two_rows(seed):
    """rebuild_card writes exactly two product_card rows (ru + ro)."""
    await seed["build_tree"]()
    session = seed["session"]
    service: CatalogService = seed["service"]
    Media = seed["models"]["Media"]

    await seed["add_product"](
        session,
        product_id=12,
        category_id=2,
        code="P-CARD",
        price=Decimal("150.00"),
        old_price=Decimal("199.00"),
        qty=7,
        slugs={"ru": "kartochka-ru", "ro": "kartochka-ro"},
        names={"ru": "Карточка", "ro": "Cartela"},
    )
    await session.flush()
    session.add(Media(id=1, product_id=12, url="http://img/main.jpg", position=0))
    session.add(Media(id=2, product_id=12, url="http://img/second.jpg", position=1))
    await session.flush()

    written = await service.rebuild_card(12)

    assert written == 2
    rows = (
        (await session.execute(select(ProductCard).where(ProductCard.product_id == 12)))
        .scalars()
        .all()
    )
    assert {row.lang for row in rows} == {"ru", "ro"}
    for row in rows:
        assert row.in_stock is True
        assert row.main_image_url == "http://img/main.jpg"
        assert row.badge == "sale"
        assert row.path == [1, 2]
        assert row.is_active is True


@pytest.mark.asyncio
async def test_rebuild_card_inactive_writes_zero(seed):
    """An inactive product gets no card rows (hidden from listings)."""
    await seed["build_tree"]()
    session = seed["session"]
    service: CatalogService = seed["service"]

    await seed["add_product"](
        session,
        product_id=13,
        category_id=2,
        code="P-INACTIVE",
        price=Decimal("50.00"),
        qty=0,
        slugs={"ru": "off-ru", "ro": "off-ro"},
        names={"ru": "Выкл", "ro": "Off"},
        is_active=False,
    )
    await session.flush()

    written = await service.rebuild_card(13)

    assert written == 0
    rows = (
        (await session.execute(select(ProductCard).where(ProductCard.product_id == 13)))
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_rebuild_card_active_incomplete_raises(seed):
    """rebuild_card on an active but single-lang product raises PublicationError."""
    await seed["build_tree"]()
    session = seed["session"]
    service: CatalogService = seed["service"]

    await seed["add_product"](
        session,
        product_id=14,
        category_id=2,
        code="P-INCOMPLETE",
        price=Decimal("77.00"),
        qty=1,
        slugs={"ru": "incomplete-ru"},
        names={"ru": "Неполный"},
        is_active=True,
    )
    await session.flush()

    with pytest.raises(PublicationError):
        await service.rebuild_card(14)


async def _seed_counted_products(seed) -> None:
    """One product directly in the root, one in its child (both publishable)."""
    session = seed["session"]
    service: CatalogService = seed["service"]
    for product_id, category_id in ((201, 1), (202, 2)):
        await seed["add_product"](
            session,
            product_id=product_id,
            category_id=category_id,
            code=f"P-CNT-{product_id}",
            price=Decimal("50.00"),
            qty=3,
            slugs={"ru": f"cnt-{product_id}-ru", "ro": f"cnt-{product_id}-ro"},
            names={"ru": f"Товар {product_id}", "ro": f"Produs {product_id}"},
        )
    await session.flush()
    await service.rebuild_cards([201, 202])


@pytest.mark.asyncio
async def test_category_tree_counts_roll_up_over_subtree(seed):
    """A parent counts its own products plus everything under it."""
    await seed["build_tree"]()
    await _seed_counted_products(seed)
    service: CatalogService = seed["service"]

    root = (await service.list_categories("ru"))[0]

    assert root.product_count == 2
    assert root.children[0].product_count == 1


@pytest.mark.asyncio
async def test_category_tree_counts_are_per_language(seed):
    """Each language is counted separately — two card rows are not one product twice."""
    await seed["build_tree"]()
    await _seed_counted_products(seed)
    service: CatalogService = seed["service"]

    ru_root = (await service.list_categories("ru"))[0]
    ro_root = (await service.list_categories("ro"))[0]

    assert ru_root.product_count == 2
    assert ro_root.product_count == 2


@pytest.mark.asyncio
async def test_category_tree_counts_exclude_inactive_products(seed):
    """Deactivated products drop out of the count (their cards are removed)."""
    await seed["build_tree"]()
    await _seed_counted_products(seed)
    session = seed["session"]
    service: CatalogService = seed["service"]

    product = await service.repo.get_product(202)
    product.is_active = False
    await session.flush()
    await service.rebuild_card(202)

    root = (await service.list_categories("ru"))[0]

    assert root.product_count == 1
    assert root.children[0].product_count == 0


@pytest.mark.asyncio
async def test_category_tree_counts_zero_without_products(seed):
    """An empty tree reports zeros rather than omitting the field."""
    await seed["build_tree"]()
    service: CatalogService = seed["service"]

    root = (await service.list_categories("ru"))[0]

    assert root.product_count == 0
    assert root.children[0].product_count == 0


@pytest.mark.asyncio
async def test_category_detail_children_carry_counts(seed):
    """Child chips on the category page get the same subtree counts."""
    await seed["build_tree"]()
    await _seed_counted_products(seed)
    service: CatalogService = seed["service"]

    detail = await service.get_category("electronika", "ru")

    assert [child.product_count for child in detail.children] == [1]
