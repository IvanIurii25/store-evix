"""Cart business logic (stage B5, §2.3 / §7).

Holds the rules the router and repository must not: cart resolution / creation for
guests (``session_token``) and users, add/increment/update/remove of lines, live
recomputation of prices and totals from ``product.price`` (money is never stored
on the cart — §2.3), and the guest→user merge on login (§7).

Robustness rule (B5 DoD): a removed or deactivated product must not break
``GET /cart``. Deleted products are dropped by the repository's inner join;
deactivated products (``is_active = False``) are skipped here. Neither raises.

The service knows nothing about HTTP; it raises domain errors that the router
maps to responses, and it commits its own unit of work (add/update/remove/merge).
"""

from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.lang import normalize_lang
from app.models.cart import Cart
from app.models.catalog import Product
from app.repositories.cart_repo import NAME_LANG, CartRepository
from app.schemas.cart import CartItemOut, CartOut


class CartError(Exception):
    """Base class for cart domain errors (mapped to HTTP by the router)."""


class ProductNotAvailableError(CartError):
    """The requested product does not exist or is not active for purchase."""


class ItemNotFoundError(CartError):
    """The addressed line is not present in the cart."""


class CartService:
    """Cart use-cases bound to one session (§3 service layer)."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and its repository.

        Args:
            session: Active async session (request-scoped or test-scoped).
        """
        self.session = session
        self.repo = CartRepository(session)

    # ------------------------------------------------------------------ #
    # Cart resolution
    # ------------------------------------------------------------------ #
    async def _get_cart(
        self,
        user_id: int | None,
        session_token: UUID | None,
    ) -> Cart | None:
        """Resolve the active cart for a user or a guest without creating one.

        Args:
            user_id: Owning user id, or ``None`` for a guest.
            session_token: Guest cookie token, or ``None`` for a user.

        Returns:
            Cart | None: The active cart, or ``None`` if none exists yet.
        """
        if user_id is not None:
            return await self.repo.get_cart_by_user(user_id)
        if session_token is not None:
            return await self.repo.get_cart_by_session_token(session_token)
        return None

    async def _get_or_create_cart(
        self,
        user_id: int | None,
        session_token: UUID | None,
    ) -> Cart:
        """Resolve the active cart, creating one for the caller if absent.

        Args:
            user_id: Owning user id, or ``None`` for a guest.
            session_token: Guest cookie token, or ``None`` for a user.

        Returns:
            Cart: The existing or freshly created active cart.
        """
        cart = await self._get_cart(user_id, session_token)
        if cart is not None:
            return cart
        owner_user_id = user_id
        owner_token = None if user_id is not None else session_token
        return await self.repo.create_cart(owner_user_id, owner_token)

    # ------------------------------------------------------------------ #
    # Read (live-priced)
    # ------------------------------------------------------------------ #
    async def get_cart(
        self,
        user_id: int | None,
        session_token: UUID | None,
        lang: str = NAME_LANG,
    ) -> CartOut:
        """Return the cart with prices/totals recomputed from ``product.price``.

        Deleted products are already dropped by the repository join; deactivated
        products (``is_active = False``) are skipped here so a stale line never
        breaks the response (§2.3, B5 DoD).

        Args:
            user_id: Owning user id, or ``None`` for a guest.
            session_token: Guest cookie token, or ``None`` for a guest with no
                cart yet.
            lang: Requested display language; normalized to a supported code
                (unsupported / ``None`` fall back to :data:`NAME_LANG`).

        Returns:
            CartOut: Live-priced lines plus subtotal and item count (empty when
                no cart exists).
        """
        cart = await self._get_cart(user_id, session_token)
        if cart is None:
            return CartOut(items=[], subtotal=Decimal("0"), item_count=0)
        return await self._render(cart.id, lang)

    async def _render(self, cart_id: int, lang: str = NAME_LANG) -> CartOut:
        """Build a :class:`CartOut` from live product data for a cart.

        Args:
            cart_id: Cart id to render.
            lang: Requested display language; normalized to a supported code
                before querying the localized name.

        Returns:
            CartOut: The recomputed cart projection.
        """
        rows = await self.repo.list_items(cart_id, normalize_lang(lang))
        items: list[CartItemOut] = []
        subtotal = Decimal("0")
        item_count = 0
        for line, product, name in rows:
            if not product.is_active:
                continue
            line_total = product.price * line.qty
            subtotal += line_total
            item_count += line.qty
            items.append(
                CartItemOut(
                    product_id=product.id,
                    name=name if name is not None else product.code,
                    price=product.price,
                    qty=line.qty,
                    line_total=line_total,
                )
            )
        return CartOut(items=items, subtotal=subtotal, item_count=item_count)

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #
    async def add_item(
        self,
        user_id: int | None,
        session_token: UUID | None,
        product_id: int,
        qty: int,
        lang: str = NAME_LANG,
    ) -> CartOut:
        """Add a product to the cart, or increment its ``qty`` if already present.

        Args:
            user_id: Owning user id, or ``None`` for a guest.
            session_token: Guest cookie token (required for a guest).
            product_id: Catalog product id to add.
            qty: Quantity to add.
            lang: Requested display language for the re-rendered cart.

        Returns:
            CartOut: The re-rendered cart.

        Raises:
            ProductNotAvailableError: If the product is missing or inactive.
        """
        product = await self._require_active_product(product_id)
        cart = await self._get_or_create_cart(user_id, session_token)
        await self.repo.add_or_increment_item(cart.id, product.id, qty)
        rendered = await self._render(cart.id, lang)
        await self.session.commit()
        return rendered

    async def update_item(
        self,
        user_id: int | None,
        session_token: UUID | None,
        product_id: int,
        qty: int,
        lang: str = NAME_LANG,
    ) -> CartOut:
        """Set an absolute quantity on an existing cart line.

        Args:
            user_id: Owning user id, or ``None`` for a guest.
            session_token: Guest cookie token.
            product_id: Catalog product id whose line is being changed.
            qty: New absolute quantity.
            lang: Requested display language for the re-rendered cart.

        Returns:
            CartOut: The re-rendered cart.

        Raises:
            ItemNotFoundError: If the cart or line does not exist.
        """
        cart = await self._get_cart(user_id, session_token)
        if cart is None:
            raise ItemNotFoundError("Cart is empty")
        updated = await self.repo.set_item_qty(cart.id, product_id, qty)
        if updated is None:
            raise ItemNotFoundError("Item not in cart")
        rendered = await self._render(cart.id, lang)
        await self.session.commit()
        return rendered

    async def remove_item(
        self,
        user_id: int | None,
        session_token: UUID | None,
        product_id: int,
        lang: str = NAME_LANG,
    ) -> CartOut:
        """Remove a line from the cart.

        Args:
            user_id: Owning user id, or ``None`` for a guest.
            session_token: Guest cookie token.
            product_id: Catalog product id whose line is removed.
            lang: Requested display language for the re-rendered cart.

        Returns:
            CartOut: The re-rendered cart.

        Raises:
            ItemNotFoundError: If the cart or line does not exist.
        """
        cart = await self._get_cart(user_id, session_token)
        if cart is None:
            raise ItemNotFoundError("Cart is empty")
        removed = await self.repo.delete_item(cart.id, product_id)
        if not removed:
            raise ItemNotFoundError("Item not in cart")
        rendered = await self._render(cart.id, lang)
        await self.session.commit()
        return rendered

    # ------------------------------------------------------------------ #
    # Merge (guest -> user, §7)
    # ------------------------------------------------------------------ #
    async def merge_guest_into_user(
        self,
        user_id: int,
        session_token: UUID,
        lang: str = NAME_LANG,
    ) -> CartOut:
        """Merge a guest cart (by ``session_token``) into the user's cart (§7).

        Called after login. Quantities are summed for products present in both
        carts (``unique(cart_id, product_id)`` is preserved); guest-only lines are
        moved as-is. The emptied guest cart is deleted. A no-op if there is no
        guest cart.

        Args:
            user_id: The now-authenticated user's id.
            session_token: The guest cookie token to merge from.
            lang: Requested display language for the re-rendered cart.

        Returns:
            CartOut: The re-rendered user cart.
        """
        guest_cart = await self.repo.get_cart_by_session_token(session_token)
        user_cart = await self._get_or_create_cart(user_id, None)
        if guest_cart is None or guest_cart.id == user_cart.id:
            rendered = await self._render(user_cart.id, lang)
            await self.session.commit()
            return rendered

        for guest_line in await self.repo.list_raw_items(guest_cart.id):
            target = await self.repo.get_item(user_cart.id, guest_line.product_id)
            if target is not None:
                target.qty += guest_line.qty
                await self.repo.delete_item(guest_cart.id, guest_line.product_id)
            else:
                await self.repo.move_item_to_cart(guest_line, user_cart.id)
        await self.repo.delete_cart(guest_cart.id)

        rendered = await self._render(user_cart.id, lang)
        await self.session.commit()
        return rendered

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _require_active_product(self, product_id: int) -> Product:
        """Return an active product or raise, so inactive stock cannot be added.

        Args:
            product_id: Catalog product id.

        Returns:
            Product: The active product.

        Raises:
            ProductNotAvailableError: If the product is missing or inactive.
        """
        product = await self.session.get(Product, product_id)
        if product is None or not product.is_active:
            raise ProductNotAvailableError("Product not available")
        return product
