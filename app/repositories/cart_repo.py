"""Async data-access layer for the cart domain (stage B5, §2.3).

Only SQL / query construction lives here — no business rules (those belong to
:class:`~app.services.cart_service.CartService`). All queries are async
SQLAlchemy 2.0 ``select()`` / ``update()`` / ``delete()`` statements bound to one
session.

The cart never stores money: :meth:`CartRepository.list_items` joins each line to
its ``product`` so the service can compute prices/totals from the live
``product.price`` (§2.3). ``unique(cart_id, product_id)`` means a repeated product
increments ``qty`` on the existing row rather than inserting a duplicate.
"""

from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.cart import Cart, CartItem
from app.models.catalog import Product, ProductTranslation

# Status of a live, still-editable cart (§2.3). Only ``draft`` carts are served /
# merged; ``converted`` / ``abandoned`` carts are historical.
ACTIVE_STATUS: str = "draft"

# Fallback language for a line's display name when resolving ``product_translation``
# (the cart API is language-agnostic; §2.6 default is ``ro``).
NAME_LANG: str = "ro"


class CartRepository:
    """Read/write queries for cart entities, bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used by every query method.

        Args:
            session: Active async session (request-scoped or test-scoped).
        """
        self.session = session

    # ------------------------------------------------------------------ #
    # Cart resolution
    # ------------------------------------------------------------------ #
    async def get_cart_by_user(self, user_id: int) -> Cart | None:
        """Return the active (``draft``) cart for a user, if any.

        Args:
            user_id: Owning user id.

        Returns:
            Cart | None: The user's active cart, or ``None``.
        """
        stmt = select(Cart).where(
            Cart.user_id == user_id,
            Cart.status == ACTIVE_STATUS,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_cart_by_session_token(self, session_token: UUID) -> Cart | None:
        """Return the active (``draft``) guest cart for a session token, if any.

        Args:
            session_token: Guest cookie token.

        Returns:
            Cart | None: The guest's active cart, or ``None``.
        """
        stmt = select(Cart).where(
            Cart.session_token == session_token,
            Cart.status == ACTIVE_STATUS,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create_cart(
        self,
        user_id: int | None,
        session_token: UUID | None,
    ) -> Cart:
        """Create and flush a new ``draft`` cart for a user or a guest.

        Exactly one of ``user_id`` / ``session_token`` identifies the owner; the
        caller (service) enforces which is set.

        Args:
            user_id: Owning user id, or ``None`` for a guest cart.
            session_token: Guest cookie token, or ``None`` for a user cart.

        Returns:
            Cart: The persisted cart (flushed, ``id`` assigned).
        """
        cart = Cart(
            user_id=user_id,
            session_token=session_token,
            status=ACTIVE_STATUS,
        )
        self.session.add(cart)
        await self.session.flush()
        return cart

    # ------------------------------------------------------------------ #
    # Items
    # ------------------------------------------------------------------ #
    async def get_item(self, cart_id: int, product_id: int) -> CartItem | None:
        """Return a single cart line by ``(cart_id, product_id)``, if present.

        Args:
            cart_id: Cart id.
            product_id: Catalog product id.

        Returns:
            CartItem | None: The line, or ``None``.
        """
        stmt = select(CartItem).where(
            CartItem.cart_id == cart_id,
            CartItem.product_id == product_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add_or_increment_item(
        self,
        cart_id: int,
        product_id: int,
        qty: int,
    ) -> CartItem:
        """Add a line, or increment ``qty`` if the product is already in the cart.

        Honours ``unique(cart_id, product_id)`` (§2.3): a repeat never inserts a
        duplicate row.

        Args:
            cart_id: Cart id.
            product_id: Catalog product id.
            qty: Quantity to add.

        Returns:
            CartItem: The created or updated line (flushed).
        """
        existing = await self.get_item(cart_id, product_id)
        if existing is not None:
            existing.qty += qty
            await self.session.flush()
            return existing
        item = CartItem(cart_id=cart_id, product_id=product_id, qty=qty)
        self.session.add(item)
        await self.session.flush()
        return item

    async def set_item_qty(
        self,
        cart_id: int,
        product_id: int,
        qty: int,
    ) -> CartItem | None:
        """Set an absolute ``qty`` on an existing line; ``None`` if it is absent.

        Args:
            cart_id: Cart id.
            product_id: Catalog product id.
            qty: New absolute quantity.

        Returns:
            CartItem | None: The updated line, or ``None`` if no such line.
        """
        item = await self.get_item(cart_id, product_id)
        if item is None:
            return None
        item.qty = qty
        await self.session.flush()
        return item

    async def delete_item(self, cart_id: int, product_id: int) -> bool:
        """Remove a line by ``(cart_id, product_id)``.

        Args:
            cart_id: Cart id.
            product_id: Catalog product id.

        Returns:
            bool: ``True`` if a row was deleted, ``False`` if none matched.
        """
        stmt = delete(CartItem).where(
            CartItem.cart_id == cart_id,
            CartItem.product_id == product_id,
        )
        result = await self.session.execute(stmt)
        return bool(result.rowcount)

    async def list_items(
        self,
        cart_id: int,
        lang: str,
    ) -> list[tuple[CartItem, Product, str | None]]:
        """Return every line of a cart joined to its product (single query).

        The join carries the live price / active flag plus the localized display
        name so the service can compute totals and skip removed or deactivated
        products without an N+1 (§2.3). An INNER JOIN on ``product`` drops lines
        whose product row is gone entirely.

        The display name is resolved language-aware via two LEFT joins to
        ``product_translation`` — the requested ``lang`` and the default
        :data:`NAME_LANG` — coalesced so the returned name is
        ``requested-lang → default-lang → None``. A product missing the requested
        translation still renders (the service then falls back to ``product.code``
        when the name is ``None``). When ``lang == NAME_LANG`` the two joins are
        harmless (the coalesce returns the same value).

        Args:
            cart_id: Cart id.
            lang: Requested display language (validated by the caller).

        Returns:
            list[tuple[CartItem, Product, str | None]]: ``(line, product, name)``
                triples ordered by ``product_id`` for a stable response.
        """
        req = aliased(ProductTranslation)
        dft = aliased(ProductTranslation)
        stmt = (
            select(CartItem, Product, func.coalesce(req.name, dft.name))
            .join(Product, Product.id == CartItem.product_id)
            .outerjoin(req, (req.product_id == Product.id) & (req.lang == lang))
            .outerjoin(dft, (dft.product_id == Product.id) & (dft.lang == NAME_LANG))
            .where(CartItem.cart_id == cart_id)
            .order_by(CartItem.product_id)
        )
        rows = await self.session.execute(stmt)
        return [tuple(row) for row in rows.all()]

    # ------------------------------------------------------------------ #
    # Merge (guest -> user, §7)
    # ------------------------------------------------------------------ #
    async def list_raw_items(self, cart_id: int) -> list[CartItem]:
        """Return raw cart lines (no product join) for a cart.

        Used by merge, which moves lines regardless of product state.

        Args:
            cart_id: Cart id.

        Returns:
            list[CartItem]: The cart's lines.
        """
        stmt = select(CartItem).where(CartItem.cart_id == cart_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def move_item_to_cart(self, item: CartItem, target_cart_id: int) -> None:
        """Re-point an existing line to a different cart (no product it lacks).

        Args:
            item: The line to move.
            target_cart_id: Destination cart id.
        """
        item.cart_id = target_cart_id
        await self.session.flush()

    async def delete_cart(self, cart_id: int) -> None:
        """Delete an (emptied) cart row, e.g. a merged-away guest cart.

        Args:
            cart_id: Cart id.
        """
        stmt = delete(Cart).where(Cart.id == cart_id)
        await self.session.execute(stmt)
