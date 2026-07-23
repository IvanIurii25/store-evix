"""Direct service-layer tests for :class:`app.services.cart_service.CartService`.

Complements ``test_cart_api.py`` (HTTP-level) by driving the service directly
against the real DB, covering the branches the API tests don't reach: cart
resolution for the no-identity case, empty-cart reads, the ``product.code``
name fallback, update/remove not-found paths, both guest and user cart owners,
and every branch of the guest→user merge (§7).
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cart import Cart, CartItem
from app.services.cart_service import (
    CartService,
    ItemNotFoundError,
    ProductNotAvailableError,
)
from tests.factories import (
    CartItemFactory,
    create_cart,
    create_user,
    persist,
)

pytestmark = pytest.mark.asyncio

# Fixed catalog price for products seeded via ``add_product`` in these tests.
_PRICE = Decimal("10.00")
# A product id known never to be seeded — the "missing product" case.
_MISSING_PRODUCT_ID = 999_999
# Guest owner marker: neither user_id nor session_token is supplied.
_NO_USER = None
_NO_TOKEN = None


class TestCartResolution:
    """``get_cart`` / cart resolution for guests, users, and no identity."""

    async def test_get_cart_no_identity_returns_empty_cart(
        self,
        db_session: AsyncSession,
    ) -> None:
        """No user_id and no session_token resolves to a rendered empty cart."""
        # Arrange
        service = CartService(db_session)

        # Act
        result = await service.get_cart(_NO_USER, _NO_TOKEN)

        # Assert
        assert result.items == [], "empty cart must have no lines"
        assert result.subtotal == Decimal("0"), "empty subtotal must be zero"
        assert result.item_count == 0, "empty cart must count zero items"

    async def test_get_cart_guest_with_no_cart_returns_empty(
        self,
        db_session: AsyncSession,
    ) -> None:
        """A guest token with no persisted cart yields an empty cart, not an error."""
        # Arrange
        service = CartService(db_session)
        unknown_token = uuid4()

        # Act
        result = await service.get_cart(_NO_USER, unknown_token)

        # Assert
        assert result.item_count == 0, "unseen guest token yields empty cart"

    async def test_get_cart_user_reflects_persisted_line(
        self,
        db_session: AsyncSession,
        add_product,
    ) -> None:
        """A user's existing cart line is rendered with live price and totals."""
        # Arrange
        await add_product(1, price=_PRICE, code="P1")
        user = await create_user(db_session)
        cart = await create_cart(db_session, user=user)
        await persist(db_session, CartItemFactory(cart_id=cart.id, product_id=1, qty=3))
        service = CartService(db_session)

        # Act
        result = await service.get_cart(user.id, _NO_TOKEN)

        # Assert
        assert result.item_count == 3, "user cart must report line qty"
        assert result.subtotal == _PRICE * 3, "subtotal must be price*qty"


class TestCartAddItem:
    """``add_item`` — create cart, increment, and inactive-product rejection."""

    async def test_add_item_guest_creates_cart_and_line(
        self,
        db_session: AsyncSession,
        add_product,
    ) -> None:
        """A guest add with no existing cart creates one and adds the line."""
        # Arrange
        await add_product(1, price=_PRICE, code="P1")
        token = uuid4()
        service = CartService(db_session)

        # Act
        result = await service.add_item(_NO_USER, token, product_id=1, qty=2)

        # Assert
        assert result.item_count == 2, "added qty must be reflected"
        carts = (await db_session.execute(select(Cart))).scalars().all()
        assert len(carts) == 1, "exactly one guest cart is created"
        assert carts[0].session_token == token, "cart is owned by the guest token"

    async def test_add_item_existing_cart_increments(
        self,
        db_session: AsyncSession,
        add_product,
    ) -> None:
        """Adding to an already-created user cart increments the same line."""
        # Arrange
        await add_product(1, price=_PRICE, code="P1")
        user = await create_user(db_session)
        service = CartService(db_session)
        await service.add_item(user.id, _NO_TOKEN, product_id=1, qty=1)

        # Act
        result = await service.add_item(user.id, _NO_TOKEN, product_id=1, qty=4)

        # Assert
        assert result.item_count == 5, "second add must increment existing line"
        assert len(result.items) == 1, "no duplicate line is created"

    async def test_add_item_missing_product_raises(
        self,
        db_session: AsyncSession,
    ) -> None:
        """Adding a product that does not exist raises ProductNotAvailableError."""
        # Arrange
        service = CartService(db_session)

        # Act / Assert
        with pytest.raises(ProductNotAvailableError):
            await service.add_item(
                _NO_USER, uuid4(), product_id=_MISSING_PRODUCT_ID, qty=1
            )

    async def test_add_item_inactive_product_raises(
        self,
        db_session: AsyncSession,
        add_product,
    ) -> None:
        """Adding an inactive product raises ProductNotAvailableError."""
        # Arrange
        await add_product(1, price=_PRICE, code="P1", is_active=False)
        service = CartService(db_session)

        # Act / Assert
        with pytest.raises(ProductNotAvailableError):
            await service.add_item(_NO_USER, uuid4(), product_id=1, qty=1)


class TestCartRender:
    """``_render`` edge cases: code fallback name and inactive-line skipping."""

    async def test_render_name_falls_back_to_code_when_no_translation(
        self,
        db_session: AsyncSession,
    ) -> None:
        """A line whose product has no translation renders ``product.code``."""
        # Arrange: seed a product WITHOUT any translation row.
        from app.models.catalog import Category, Product

        if await db_session.get(Category, 1) is None:
            await persist(
                db_session,
                Category(id=1, parent_id=None, path=[1], depth=0, is_active=True),
            )
        await persist(
            db_session,
            Product(
                id=1,
                category_id=1,
                code="BARECODE",
                price=_PRICE,
                qty=5,
                is_active=True,
            ),
        )
        user = await create_user(db_session)
        cart = await create_cart(db_session, user=user)
        await persist(db_session, CartItemFactory(cart_id=cart.id, product_id=1, qty=1))
        service = CartService(db_session)

        # Act
        result = await service.get_cart(user.id, _NO_TOKEN)

        # Assert
        assert result.items[0].name == "BARECODE", "name falls back to product.code"

    async def test_render_skips_inactive_line(
        self,
        db_session: AsyncSession,
        add_product,
    ) -> None:
        """A deactivated product's line is skipped and not counted in totals."""
        # Arrange
        await add_product(1, price=_PRICE, code="P1")
        inactive = await add_product(2, price=Decimal("99.00"), code="P2")
        user = await create_user(db_session)
        cart = await create_cart(db_session, user=user)
        await persist(
            db_session,
            CartItemFactory(cart_id=cart.id, product_id=1, qty=1),
            CartItemFactory(cart_id=cart.id, product_id=2, qty=1),
        )
        inactive.is_active = False
        await db_session.flush()
        service = CartService(db_session)

        # Act
        result = await service.get_cart(user.id, _NO_TOKEN)

        # Assert
        assert [i.product_id for i in result.items] == [1], "inactive line dropped"
        assert result.subtotal == _PRICE, "inactive line excluded from subtotal"


class TestCartUpdateItem:
    """``update_item`` — success and both not-found branches."""

    async def test_update_item_sets_absolute_qty(
        self,
        db_session: AsyncSession,
        add_product,
    ) -> None:
        """Updating an existing line sets an absolute quantity."""
        # Arrange
        await add_product(1, price=_PRICE, code="P1")
        user = await create_user(db_session)
        service = CartService(db_session)
        await service.add_item(user.id, _NO_TOKEN, product_id=1, qty=1)

        # Act
        result = await service.update_item(user.id, _NO_TOKEN, product_id=1, qty=7)

        # Assert
        assert result.items[0].qty == 7, "qty is set absolutely, not incremented"
        assert result.item_count == 7, "item_count reflects the new qty"

    async def test_update_item_no_cart_raises_not_found(
        self,
        db_session: AsyncSession,
    ) -> None:
        """Updating when the caller has no cart raises ItemNotFoundError."""
        # Arrange
        user = await create_user(db_session)
        service = CartService(db_session)

        # Act / Assert
        with pytest.raises(ItemNotFoundError):
            await service.update_item(user.id, _NO_TOKEN, product_id=1, qty=2)

    async def test_update_item_line_absent_raises_not_found(
        self,
        db_session: AsyncSession,
        add_product,
    ) -> None:
        """Updating a line not present in an existing cart raises ItemNotFoundError."""
        # Arrange: cart exists (product 1) but we update product 2.
        await add_product(1, price=_PRICE, code="P1")
        await add_product(2, price=_PRICE, code="P2")
        user = await create_user(db_session)
        service = CartService(db_session)
        await service.add_item(user.id, _NO_TOKEN, product_id=1, qty=1)

        # Act / Assert
        with pytest.raises(ItemNotFoundError):
            await service.update_item(user.id, _NO_TOKEN, product_id=2, qty=2)


class TestCartRemoveItem:
    """``remove_item`` — success and both not-found branches."""

    async def test_remove_item_deletes_line(
        self,
        db_session: AsyncSession,
        add_product,
    ) -> None:
        """Removing an existing line drops it from the cart."""
        # Arrange
        await add_product(1, price=_PRICE, code="P1")
        await add_product(2, price=_PRICE, code="P2")
        user = await create_user(db_session)
        service = CartService(db_session)
        await service.add_item(user.id, _NO_TOKEN, product_id=1, qty=1)
        await service.add_item(user.id, _NO_TOKEN, product_id=2, qty=1)

        # Act
        result = await service.remove_item(user.id, _NO_TOKEN, product_id=1)

        # Assert
        assert [i.product_id for i in result.items] == [2], "only removed line gone"

    async def test_remove_item_no_cart_raises_not_found(
        self,
        db_session: AsyncSession,
    ) -> None:
        """Removing when the caller has no cart raises ItemNotFoundError."""
        # Arrange
        service = CartService(db_session)

        # Act / Assert
        with pytest.raises(ItemNotFoundError):
            await service.remove_item(_NO_USER, uuid4(), product_id=1)

    async def test_remove_item_line_absent_raises_not_found(
        self,
        db_session: AsyncSession,
        add_product,
    ) -> None:
        """Removing a line not present in an existing cart raises ItemNotFoundError."""
        # Arrange
        await add_product(1, price=_PRICE, code="P1")
        user = await create_user(db_session)
        service = CartService(db_session)
        await service.add_item(user.id, _NO_TOKEN, product_id=1, qty=1)

        # Act / Assert
        with pytest.raises(ItemNotFoundError):
            await service.remove_item(
                user.id, _NO_TOKEN, product_id=_MISSING_PRODUCT_ID
            )


class TestCartMerge:
    """``merge_guest_into_user`` — every branch of the §7 login merge."""

    async def test_merge_sums_and_moves_lines(
        self,
        db_session: AsyncSession,
        add_product,
    ) -> None:
        """Matching products sum; guest-only lines move; guest cart is deleted."""
        # Arrange
        await add_product(1, price=_PRICE, code="P1")
        await add_product(2, price=Decimal("20.00"), code="P2")
        user = await create_user(db_session)
        guest_token = uuid4()
        guest_cart = await create_cart(db_session, session_token=guest_token)
        user_cart = await create_cart(db_session, user=user)
        await persist(
            db_session,
            CartItemFactory(cart_id=guest_cart.id, product_id=1, qty=2),
            CartItemFactory(cart_id=guest_cart.id, product_id=2, qty=5),
            CartItemFactory(cart_id=user_cart.id, product_id=1, qty=3),
        )
        service = CartService(db_session)

        # Act
        result = await service.merge_guest_into_user(user.id, guest_token)

        # Assert
        by_id = {i.product_id: i for i in result.items}
        assert by_id[1].qty == 5, "matching product qty is summed (3+2)"
        assert by_id[2].qty == 5, "guest-only line is moved intact"
        remaining = (await db_session.execute(select(Cart))).scalars().all()
        assert len(remaining) == 1, "guest cart is deleted after merge"
        assert remaining[0].user_id == user.id, "surviving cart belongs to the user"

    async def test_merge_no_guest_cart_is_noop(
        self,
        db_session: AsyncSession,
        add_product,
    ) -> None:
        """With no guest cart, merge returns the (created) user cart unchanged."""
        # Arrange
        await add_product(1, price=_PRICE, code="P1")
        user = await create_user(db_session)
        user_cart = await create_cart(db_session, user=user)
        await persist(
            db_session, CartItemFactory(cart_id=user_cart.id, product_id=1, qty=4)
        )
        service = CartService(db_session)

        # Act: pass a token that has no guest cart.
        result = await service.merge_guest_into_user(user.id, uuid4())

        # Assert
        assert result.item_count == 4, "user cart is returned untouched"

    async def test_merge_creates_user_cart_when_absent(
        self,
        db_session: AsyncSession,
        add_product,
    ) -> None:
        """Merge with no user cart creates one and folds the guest cart into it."""
        # Arrange: only a guest cart exists.
        await add_product(1, price=_PRICE, code="P1")
        user = await create_user(db_session)
        guest_token = uuid4()
        guest_cart = await create_cart(db_session, session_token=guest_token)
        await persist(
            db_session, CartItemFactory(cart_id=guest_cart.id, product_id=1, qty=6)
        )
        service = CartService(db_session)

        # Act
        result = await service.merge_guest_into_user(user.id, guest_token)

        # Assert
        assert result.item_count == 6, "guest line moved into fresh user cart"
        rows = (
            (await db_session.execute(select(CartItem).where(CartItem.product_id == 1)))
            .scalars()
            .all()
        )
        assert len(rows) == 1, "the guest line was moved, not duplicated"

    async def test_merge_same_cart_is_noop(
        self,
        db_session: AsyncSession,
        add_product,
    ) -> None:
        """A cart owned by the user AND bearing the token merges into itself (no-op)."""
        # Arrange: one cart carries both user_id and the session_token.
        await add_product(1, price=_PRICE, code="P1")
        user = await create_user(db_session)
        token = uuid4()
        cart = await create_cart(db_session, user=user, session_token=token)
        await persist(db_session, CartItemFactory(cart_id=cart.id, product_id=1, qty=2))
        service = CartService(db_session)

        # Act
        result = await service.merge_guest_into_user(user.id, token)

        # Assert
        assert result.item_count == 2, "self-merge leaves the cart unchanged"
        remaining = (await db_session.execute(select(Cart))).scalars().all()
        assert len(remaining) == 1, "no cart is deleted on a self-merge"
