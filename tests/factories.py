"""Shared test factories (factory_boy) + async persistence helpers.

evix-store runs on **async** SQLAlchemy 2, where ``factory_boy``'s
``SQLAlchemyModelFactory`` (sync-session bound) does not fit. So every factory
here is a plain :class:`factory.Factory` that *builds an unsaved ORM instance*
with sane defaults + sequences for unique fields — no DB access inside the
factory. Persistence is an explicit async step via :func:`persist` (or the
``create_*`` composite helpers), run against the test's transactional
``db_session``.

Usage::

    from tests.factories import ProductFactory, persist, create_product

    product = ProductFactory(category_id=cat.id)   # unsaved instance
    await persist(session, product)                # flush → id assigned

    # or the composite (category + product + ru/ro translations):
    product = await create_product(session)

Rules (test-management-guide §8): minimal required-field defaults, ``Sequence``
for unique fields, FKs passed as params, no business logic, pre-computed
password hash (one live argon2 hash per session via ``TEST_PASSWORD``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import factory

from app.core.security import hash_password
from app.models.analytics import Visit
from app.models.cart import Cart, CartItem
from app.models.catalog import (
    Attribute,
    AttributeTranslation,
    AttributeValue,
    AttributeValueTranslation,
    Category,
    CategoryTranslation,
    Media,
    Product,
    ProductAttribute,
    ProductCard,
    ProductTranslation,
)
from app.models.content_page import ContentPage, ContentPageTranslation
from app.models.order import Order, OrderItem, OrderStatusHistory
from app.models.promo import PromoCode
from app.models.restock import RestockSubscription
from app.models.settings import SiteSetting
from app.models.user import Address, AppUser

# One canonical test password + its live argon2 hash, computed once at import.
# Auth tests need a *verifiable* hash (real login), so we hash a known password
# a single time rather than per-test (argon2 is deliberately slow).
TEST_PASSWORD: str = "Test-Password-123"
TEST_PASSWORD_HASH: str = hash_password(TEST_PASSWORD)


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
class CategoryFactory(factory.Factory):
    """Unsaved :class:`Category`. ``path``/``depth`` default to a flat root."""

    class Meta:
        model = Category

    path = factory.LazyFunction(list)
    depth = 0
    is_active = True
    position = 0


class CategoryTranslationFactory(factory.Factory):
    """Unsaved :class:`CategoryTranslation`; pass ``category_id`` + ``lang``."""

    class Meta:
        model = CategoryTranslation

    lang = "ru"
    name = factory.Sequence(lambda n: f"Category {n}")
    slug = factory.Sequence(lambda n: f"category-{n}")


class ProductFactory(factory.Factory):
    """Unsaved :class:`Product`; pass ``category_id``."""

    class Meta:
        model = Product

    code = factory.Sequence(lambda n: f"SKU{n:06d}")
    price = Decimal("100.00")
    qty = 10
    is_active = True


class ProductTranslationFactory(factory.Factory):
    """Unsaved :class:`ProductTranslation`; pass ``product_id`` + ``lang``."""

    class Meta:
        model = ProductTranslation

    lang = "ru"
    name = factory.Sequence(lambda n: f"Product {n}")
    slug = factory.Sequence(lambda n: f"product-{n}")


class ProductCardFactory(factory.Factory):
    """Unsaved :class:`ProductCard` read-model row; pass ``product_id``."""

    class Meta:
        model = ProductCard

    lang = "ru"
    category_id = 1
    name = factory.Sequence(lambda n: f"Product {n}")
    slug = factory.Sequence(lambda n: f"product-card-{n}")
    price = Decimal("100.00")
    in_stock = True
    path = factory.LazyFunction(list)
    is_active = True
    is_featured = False


class MediaFactory(factory.Factory):
    """Unsaved :class:`Media` image row; pass ``product_id``."""

    class Meta:
        model = Media

    url = factory.Sequence(lambda n: f"https://media.test/img-{n}.jpg")
    kind = "image"
    position = 0


class AttributeFactory(factory.Factory):
    class Meta:
        model = Attribute

    code = factory.Sequence(lambda n: f"attr-{n}")


class AttributeTranslationFactory(factory.Factory):
    class Meta:
        model = AttributeTranslation

    lang = "ru"
    name = factory.Sequence(lambda n: f"Attribute {n}")


class AttributeValueFactory(factory.Factory):
    class Meta:
        model = AttributeValue


class AttributeValueTranslationFactory(factory.Factory):
    class Meta:
        model = AttributeValueTranslation

    lang = "ru"
    value = factory.Sequence(lambda n: f"value-{n}")


class ProductAttributeFactory(factory.Factory):
    """Unsaved M2M row; pass ``product_id`` + ``value_id``."""

    class Meta:
        model = ProductAttribute


# --------------------------------------------------------------------------- #
# User
# --------------------------------------------------------------------------- #
class UserFactory(factory.Factory):
    """Unsaved :class:`AppUser` with a verifiable :data:`TEST_PASSWORD` hash."""

    class Meta:
        model = AppUser

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    password_hash = TEST_PASSWORD_HASH
    is_active = True
    is_staff = False


class StaffFactory(UserFactory):
    """Unsaved staff :class:`AppUser` (``is_staff=True``)."""

    email = factory.Sequence(lambda n: f"staff{n}@example.com")
    is_staff = True


class AddressFactory(factory.Factory):
    """Unsaved :class:`Address`; pass ``user_id``."""

    class Meta:
        model = Address

    full_name = "Test Buyer"
    phone = "+37360000000"
    city = "Chișinău"
    street = "str. Testului 1"
    is_default = False


# --------------------------------------------------------------------------- #
# Cart
# --------------------------------------------------------------------------- #
class CartFactory(factory.Factory):
    """Unsaved :class:`Cart`; pass ``user_id`` or leave guest."""

    class Meta:
        model = Cart

    status = "draft"


class CartItemFactory(factory.Factory):
    """Unsaved :class:`CartItem`; pass ``cart_id`` + ``product_id``."""

    class Meta:
        model = CartItem

    qty = 1


# --------------------------------------------------------------------------- #
# Order
# --------------------------------------------------------------------------- #
class OrderFactory(factory.Factory):
    """Unsaved :class:`Order` (pickup/COD defaults)."""

    class Meta:
        model = Order

    number = factory.Sequence(lambda n: f"TEST-{n:06d}")
    email = factory.Sequence(lambda n: f"buyer{n}@example.com")
    phone = "+37360000000"
    subtotal = Decimal("100.00")
    total = Decimal("100.00")
    delivery_type = "pickup"


class OrderItemFactory(factory.Factory):
    """Unsaved :class:`OrderItem` snapshot; pass ``order_id``."""

    class Meta:
        model = OrderItem

    name_snapshot = factory.Sequence(lambda n: f"Product {n}")
    price_snapshot = Decimal("100.00")
    qty = 1


class OrderStatusHistoryFactory(factory.Factory):
    """Unsaved :class:`OrderStatusHistory`; pass ``order_id``."""

    class Meta:
        model = OrderStatusHistory

    from_status = "new"
    to_status = "confirmed"
    changed_at = factory.LazyFunction(lambda: datetime.now(UTC))
    changed_by = "staff@test"


# --------------------------------------------------------------------------- #
# Restock / content / settings / promo / analytics
# --------------------------------------------------------------------------- #
class RestockSubscriptionFactory(factory.Factory):
    """Unsaved :class:`RestockSubscription`; pass ``product_id`` + ``user_id``."""

    class Meta:
        model = RestockSubscription

    lang = "ru"
    status = "active"


class ContentPageFactory(factory.Factory):
    class Meta:
        model = ContentPage

    slug = factory.Sequence(lambda n: f"page-{n}")
    is_published = True
    show_in_footer = True
    position = 0


class ContentPageTranslationFactory(factory.Factory):
    class Meta:
        model = ContentPageTranslation

    lang = "ru"
    title = factory.Sequence(lambda n: f"Page {n}")
    body = "# Body\n\nContent."


class SiteSettingFactory(factory.Factory):
    class Meta:
        model = SiteSetting

    key = factory.Sequence(lambda n: f"key-{n}")
    value = ""


class PromoCodeFactory(factory.Factory):
    class Meta:
        model = PromoCode

    code = factory.Sequence(lambda n: f"PROMO{n}")
    discount_type = "percent"
    discount_value = Decimal("10.00")
    active_from = factory.LazyFunction(lambda: datetime.now(UTC) - timedelta(days=1))
    active_to = factory.LazyFunction(lambda: datetime.now(UTC) + timedelta(days=30))
    is_active = True


class VisitFactory(factory.Factory):
    class Meta:
        model = Visit

    path = "/ro"
    session_id = factory.Sequence(lambda n: f"sess-{n}")
    device = "desktop"
    is_bot = False


# --------------------------------------------------------------------------- #
# Async persistence helpers
# --------------------------------------------------------------------------- #
async def persist(session, *objs):
    """Add ``objs`` to ``session`` and flush so PKs/defaults are populated.

    Args:
        session: The transactional test :class:`AsyncSession`.
        *objs: Unsaved ORM instances (e.g. built by a factory).

    Returns:
        The same ``objs`` (now flushed) as a tuple, or the single object when
        exactly one was passed — convenient for ``x = await persist(s, X())``.
    """
    session.add_all(objs)
    await session.flush()
    return objs[0] if len(objs) == 1 else objs


async def create_user(session, **kwargs) -> AppUser:
    """Persist an :class:`AppUser` (defaults from :class:`UserFactory`)."""
    return await persist(session, UserFactory(**kwargs))


async def create_staff(session, **kwargs) -> AppUser:
    """Persist a staff :class:`AppUser` (``is_staff=True``)."""
    return await persist(session, StaffFactory(**kwargs))


async def create_category(
    session, *, langs: tuple[str, ...] = ("ru", "ro"), **kwargs
) -> Category:
    """Persist a :class:`Category` plus one translation per language.

    ``path`` defaults to ``[category.id]`` (flat root) once the id is known,
    unless the caller passed an explicit ``path``.
    """
    explicit_path = "path" in kwargs
    category = await persist(session, CategoryFactory(**kwargs))
    if not explicit_path:
        category.path = [category.id]
        await session.flush()
    for lang in langs:
        await persist(
            session, CategoryTranslationFactory(category_id=category.id, lang=lang)
        )
    return category


async def create_product(
    session,
    *,
    category: Category | None = None,
    langs: tuple[str, ...] = ("ru", "ro"),
    **kwargs,
) -> Product:
    """Persist a :class:`Product` (+ its category if absent) + translations.

    Args:
        session: Transactional test session.
        category: Existing category to attach to; a fresh one is created when
            omitted.
        langs: Languages to create ``ProductTranslation`` rows for.
        **kwargs: Overrides forwarded to :class:`ProductFactory` (e.g. ``qty``).

    Returns:
        The persisted :class:`Product`.
    """
    if category is None:
        category = await create_category(session, langs=langs)
    product = await persist(session, ProductFactory(category_id=category.id, **kwargs))
    for lang in langs:
        await persist(
            session, ProductTranslationFactory(product_id=product.id, lang=lang)
        )
    return product


async def create_cart(
    session, *, user: AppUser | None = None, guest: bool = False, **kwargs
) -> Cart:
    """Persist a :class:`Cart` for ``user`` (or a guest cart with a token)."""
    if user is not None:
        kwargs.setdefault("user_id", user.id)
    if guest:
        kwargs.setdefault("session_token", uuid4())
    return await persist(session, CartFactory(**kwargs))


async def create_order(
    session,
    *,
    user: AppUser | None = None,
    items: int = 1,
    product: Product | None = None,
    **kwargs,
) -> Order:
    """Persist an :class:`Order` with ``items`` snapshot line(s).

    Args:
        session: Transactional test session.
        user: Optional owner; ``None`` → guest order.
        items: Number of :class:`OrderItem` rows to attach.
        product: Product to reference on the lines (created if omitted and
            ``items > 0``).
        **kwargs: Overrides forwarded to :class:`OrderFactory`.

    Returns:
        The persisted :class:`Order`.
    """
    if user is not None:
        kwargs.setdefault("user_id", user.id)
        kwargs.setdefault("email", user.email)
    order = await persist(session, OrderFactory(**kwargs))
    if items > 0 and product is None:
        product = await create_product(session)
    for _ in range(items):
        await persist(
            session,
            OrderItemFactory(order_id=order.id, product_id=product.id),
        )
    return order


async def create_restock_subscription(
    session, *, product: Product, user: AppUser, **kwargs
) -> RestockSubscription:
    """Persist a :class:`RestockSubscription` linking ``user`` to ``product``."""
    return await persist(
        session,
        RestockSubscriptionFactory(product_id=product.id, user_id=user.id, **kwargs),
    )


async def create_content_page(
    session, *, langs: tuple[str, ...] = ("ru", "ro"), **kwargs
) -> ContentPage:
    """Persist a :class:`ContentPage` plus one translation per language."""
    page = await persist(session, ContentPageFactory(**kwargs))
    for lang in langs:
        await persist(
            session, ContentPageTranslationFactory(page_id=page.id, lang=lang)
        )
    return page
