"""Checkout business logic: quote + atomic order creation (stage B6, §9).

Holds the rules the router and repository must not: loading the caller's cart,
server-side stock validation, delivery / discount / total computation, and the
single-transaction creation of an order with snapshotted lines, race-safe stock
decrement, cart conversion and initial status history (§9).

Discounts: promo is NOT in v1 → ``discount_total`` is always 0 (§9.3). Tax: the
``order`` table has no tax column, so tax is NOT persisted here (deferred, §9.5).

The service knows nothing about HTTP; it raises domain errors the router maps to
responses, and it owns its unit of work (one commit for the whole checkout, with
the confirmation-email stub fired only after that commit — §9.8).
"""

import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.lang import normalize_lang
from app.core.config import settings
from app.core.email import send_order_confirmation
from app.models.cart import Cart
from app.models.order import Order, OrderItem
from app.models.user import Address
from app.repositories.order_repo import NAME_LANG, OrderRepository
from app.repositories.user_repo import UserRepository
from app.schemas.order import DeliveryAddressIn, OrderItemOut, OrderOut, QuoteOut

logger = logging.getLogger(__name__)

# Delivery types (§9.4). ``courier`` requires an address; ``pickup`` is free.
PICKUP: str = "pickup"
COURIER: str = "courier"

# v1 has no promo engine → discount is always zero (§9.3).
_ZERO: Decimal = Decimal("0")


class CheckoutError(Exception):
    """Base class for checkout domain errors (mapped to HTTP by the router)."""


class EmptyCartError(CheckoutError):
    """The caller has no cart, or its cart has no purchasable lines (§9.1)."""


class DeliveryAddressRequiredError(CheckoutError):
    """Courier delivery was requested without a ``delivery_address_id`` (§9.4)."""


class DeliveryAddressForbiddenError(CheckoutError):
    """A ``delivery_address_id`` was supplied that the caller may not use.

    Raised when the id resolves to no address the caller owns — the address is
    missing, belongs to another user, or the caller is a guest (guests have no
    saved addresses). Mapped to HTTP 403; existence is never leaked (the same
    error covers "not found" and "not yours").
    """


# A resolved courier snapshot: ``(name, city, street, zip)``. All ``None`` when
# no address applies (e.g. pickup, or courier with no supplied address).
_DeliverySnapshot = tuple[str | None, str | None, str | None, str | None]

# The empty snapshot (pickup, or courier without any address).
_EMPTY_SNAPSHOT: _DeliverySnapshot = (None, None, None, None)


class OutOfStockError(CheckoutError):
    """A line's requested quantity exceeds available stock (§9.2 / §9.6).

    Carries the offending ``product_id`` so the router can surface it in the
    error ``details``. Mapped to HTTP 409 with code ``out_of_stock``.
    """

    def __init__(self, product_id: int) -> None:
        self.product_id = product_id
        super().__init__(f"Insufficient stock for product {product_id}")


class _PricedLine:
    """A resolved cart line ready to price / persist (internal to checkout)."""

    __slots__ = ("product_id", "name", "price", "qty")

    def __init__(self, product_id: int, name: str, price: Decimal, qty: int) -> None:
        self.product_id = product_id
        self.name = name
        self.price = price
        self.qty = qty

    @property
    def line_total(self) -> Decimal:
        """Return ``price * qty`` for this line."""
        return self.price * self.qty


class CheckoutService:
    """Checkout use-cases bound to one session (§3 service layer)."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and its repository.

        Args:
            session: Active async session (request-scoped or test-scoped).
        """
        self.session = session
        self.repo = OrderRepository(session)
        self.user_repo = UserRepository(session)

    # ------------------------------------------------------------------ #
    # Quote (idempotent predraft — no order created)
    # ------------------------------------------------------------------ #
    async def quote(
        self,
        *,
        user_id: int | None,
        session_token: UUID | None,
        delivery_type: str,
        delivery_address_id: int | None,
        delivery_address: DeliveryAddressIn | None = None,
        lang: str = NAME_LANG,
    ) -> QuoteOut:
        """Compute the checkout totals without creating an order (§9, idempotent).

        Args:
            user_id: Owning user id, or ``None`` for a guest.
            session_token: Guest cookie token, or ``None`` for a user.
            delivery_type: ``pickup`` or ``courier``.
            delivery_address_id: Saved address id (courier; logged-in users).
            delivery_address: Inline courier address (guest or user).
            lang: Requested line-name language (does not affect totals; kept
                consistent with checkout so the quote mirrors the order).

        Returns:
            QuoteOut: The subtotal / discount / delivery / total breakdown.

        Raises:
            EmptyCartError: If the caller has no purchasable lines.
            DeliveryAddressRequiredError: If courier is chosen without an address.
            DeliveryAddressForbiddenError: If ``delivery_address_id`` is not one
                the caller owns (validated up front so the caller is not made to
                submit before learning the id is unusable).
        """
        _cart, lines = await self._load_cart_lines(user_id, session_token, lang)
        # Validate ownership even though the delivery cost is owner-independent:
        # a foreign id must not even be quotable.
        await self._resolve_delivery_snapshot(
            user_id, delivery_type, delivery_address_id, delivery_address
        )
        return self._quote_from_lines(
            lines, delivery_type, delivery_address_id, delivery_address
        )

    # ------------------------------------------------------------------ #
    # Checkout (atomic order creation)
    # ------------------------------------------------------------------ #
    async def checkout(
        self,
        *,
        user_id: int | None,
        session_token: UUID | None,
        email: str,
        phone: str,
        delivery_type: str,
        delivery_address_id: int | None,
        delivery_address: DeliveryAddressIn | None = None,
        lang: str = NAME_LANG,
    ) -> OrderOut:
        """Create an order from the caller's cart in one transaction (§9.6).

        Steps (all in one unit of work): recompute totals, generate a race-safe
        number, insert the order + snapshotted lines, decrement stock with a
        conditional UPDATE (oversell → rollback + ``OutOfStockError``), convert
        the cart, and write the initial status history. The confirmation-email
        stub fires only after ``commit`` (§9.8).

        Args:
            user_id: Owning user id, or ``None`` for a guest order.
            session_token: Guest cookie token, or ``None`` for a user.
            email: Contact email (required, incl. guests).
            phone: Contact phone (required, incl. guests).
            delivery_type: ``pickup`` or ``courier``.
            delivery_address_id: Address id (required for courier).
            lang: Requested language captured into each line's ``name_snapshot``
                (unsupported / ``None`` fall back to :data:`NAME_LANG`).

        Returns:
            OrderOut: The created order with its lines.

        Raises:
            EmptyCartError: If the caller has no purchasable lines.
            DeliveryAddressRequiredError: If courier is chosen without an address.
            DeliveryAddressForbiddenError: If ``delivery_address_id`` is not one
                the caller owns.
            OutOfStockError: If any line's stock is insufficient at commit time.
        """
        cart, lines = await self._load_cart_lines(user_id, session_token, lang)
        snap_name, snap_city, snap_street, snap_zip = (
            await self._resolve_delivery_snapshot(
                user_id, delivery_type, delivery_address_id, delivery_address
            )
        )
        totals = self._quote_from_lines(
            lines, delivery_type, delivery_address_id, delivery_address
        )

        number = await self.repo.next_order_number()
        order = self.repo.add_order(
            number=number,
            user_id=user_id,
            email=email,
            phone=phone,
            subtotal=totals.subtotal,
            discount_total=totals.discount_total,
            delivery_cost=totals.delivery_cost,
            total=totals.total,
            delivery_type=delivery_type,
            delivery_address_id=delivery_address_id,
            delivery_name=snap_name,
            delivery_city=snap_city,
            delivery_street=snap_street,
            delivery_zip=snap_zip,
        )
        await self.session.flush()  # assign order.id
        # Pull DB-side ``created_at`` (server_default) onto the instance so the
        # response DTO carries it after commit.
        await self.session.refresh(order, attribute_names=["created_at"])

        item_models: list[OrderItem] = []
        for line in lines:
            item_models.append(
                self.repo.add_order_item(
                    order_id=order.id,
                    product_id=line.product_id,
                    name_snapshot=line.name,
                    price_snapshot=line.price,
                    qty=line.qty,
                )
            )

        for line in lines:
            decremented = await self.repo.decrement_stock(line.product_id, line.qty)
            if not decremented:
                await self.session.rollback()
                raise OutOfStockError(line.product_id)

        await self.repo.mark_cart_converted(cart.id)
        self.repo.add_status_history(
            order_id=order.id,
            from_status="",
            to_status="new",
            changed_by="system",
        )
        self.repo.add_status_history(
            order_id=order.id,
            from_status="",
            to_status="pending",
            changed_by="system",
        )

        await self.session.commit()
        await self._send_confirmation(order.number, email, order.total)
        return self._build_order_out(order, item_models)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _load_cart_lines(
        self,
        user_id: int | None,
        session_token: UUID | None,
        lang: str = NAME_LANG,
    ) -> tuple[Cart, list[_PricedLine]]:
        """Load the caller's cart and its purchasable, stock-checked lines (§9.1).

        Args:
            user_id: Owning user id, or ``None`` for a guest.
            session_token: Guest cookie token, or ``None`` for a user.
            lang: Requested line-name language; normalized to a supported code
                before querying the localized name.

        Returns:
            tuple[Cart, list[_PricedLine]]: The cart and its resolved lines.

        Raises:
            EmptyCartError: If no cart exists or it has no active lines.
            OutOfStockError: If a line already exceeds stock at read time (§9.2).
        """
        cart = await self.repo.get_active_cart(user_id, session_token)
        if cart is None:
            raise EmptyCartError("Cart is empty")

        priced: list[_PricedLine] = []
        lines = await self.repo.list_cart_lines(cart.id, normalize_lang(lang))
        for cart_item, product, name in lines:
            if not product.is_active:
                continue
            if cart_item.qty > product.qty:
                raise OutOfStockError(product.id)
            priced.append(
                _PricedLine(
                    product_id=product.id,
                    name=name if name is not None else product.code,
                    price=product.price,
                    qty=cart_item.qty,
                )
            )

        if not priced:
            raise EmptyCartError("Cart has no purchasable items")
        return cart, priced

    async def _resolve_delivery_snapshot(
        self,
        user_id: int | None,
        delivery_type: str,
        delivery_address_id: int | None,
        delivery_address: DeliveryAddressIn | None,
    ) -> _DeliverySnapshot:
        """Resolve + validate the courier snapshot for the delivery choice (§9.4).

        Single source of truth for "what address does this order use", reused by
        both :meth:`quote` and :meth:`checkout` so they agree. A saved
        ``delivery_address_id`` takes precedence over an inline ``delivery_address``
        for a logged-in user (the saved, ownership-validated address is
        authoritative); a guest's inline path is untouched.

        Args:
            user_id: Owning user id, or ``None`` for a guest.
            delivery_type: ``pickup`` or ``courier``.
            delivery_address_id: Saved address id (courier; logged-in users).
            delivery_address: Inline courier address (guest or user).

        Returns:
            _DeliverySnapshot: ``(name, city, street, zip)`` for the order, all
                ``None`` when no address applies (pickup, or courier resolved
                from an inline-only address handled by the caller's fallback).

        Raises:
            DeliveryAddressForbiddenError: If ``delivery_address_id`` is supplied
                but resolves to no address the caller owns (missing, foreign, or
                guest). Existence is not leaked.
        """
        if delivery_type == PICKUP or delivery_address_id is None:
            if delivery_address is not None:
                return (
                    delivery_address.full_name,
                    delivery_address.city,
                    delivery_address.street,
                    delivery_address.zip,
                )
            return _EMPTY_SNAPSHOT

        address = await self._load_owned_address(user_id, delivery_address_id)
        return (address.full_name, address.city, address.street, address.zip)

    async def _load_owned_address(
        self,
        user_id: int | None,
        delivery_address_id: int,
    ) -> Address:
        """Load an address the caller owns, or raise (§9.4 ownership guard).

        Args:
            user_id: Owning user id, or ``None`` for a guest (always rejected —
                guests have no saved addresses).
            delivery_address_id: The requested saved-address id.

        Returns:
            Address: The address owned by ``user_id``.

        Raises:
            DeliveryAddressForbiddenError: If the caller is a guest, or the id
                resolves to no address owned by ``user_id``.
        """
        if user_id is None:
            raise DeliveryAddressForbiddenError(
                "Delivery address is not available to this caller"
            )
        address = await self.user_repo.get_address(user_id, delivery_address_id)
        if address is None:
            raise DeliveryAddressForbiddenError(
                "Delivery address is not available to this caller"
            )
        return address

    def _quote_from_lines(
        self,
        lines: list[_PricedLine],
        delivery_type: str,
        delivery_address_id: int | None,
        delivery_address: DeliveryAddressIn | None = None,
    ) -> QuoteOut:
        """Compute totals from resolved lines + delivery choice (§9.3–9.5).

        Args:
            lines: Resolved, stock-checked cart lines.
            delivery_type: ``pickup`` or ``courier``.
            delivery_address_id: Saved address id (courier; logged-in users).
            delivery_address: Inline courier address (guest or user).

        Returns:
            QuoteOut: The computed breakdown.

        Raises:
            DeliveryAddressRequiredError: If courier is chosen without an address.
        """
        subtotal = sum((line.line_total for line in lines), _ZERO)
        discount_total = _ZERO
        has_address = delivery_address_id is not None or delivery_address is not None
        delivery_cost = self._delivery_cost(subtotal, delivery_type, has_address)
        total = subtotal - discount_total + delivery_cost
        item_count = sum(line.qty for line in lines)
        return QuoteOut(
            subtotal=subtotal,
            discount_total=discount_total,
            delivery_cost=delivery_cost,
            total=total,
            delivery_type=delivery_type,
            item_count=item_count,
        )

    def _delivery_cost(
        self,
        subtotal: Decimal,
        delivery_type: str,
        has_address: bool,
    ) -> Decimal:
        """Return the delivery charge for the chosen method (§9.4).

        ``pickup`` is free. ``courier`` costs ``settings.courier_rate`` unless the
        subtotal reaches ``settings.free_delivery_from`` (when configured), and
        requires a delivery address (saved id or inline).

        Args:
            subtotal: Sum of line totals.
            delivery_type: ``pickup`` or ``courier``.
            has_address: Whether a courier address was supplied (id or inline).

        Returns:
            Decimal: The delivery cost.

        Raises:
            DeliveryAddressRequiredError: If courier is chosen without an address.
        """
        if delivery_type == PICKUP:
            return _ZERO
        if not has_address:
            raise DeliveryAddressRequiredError(
                "Courier delivery requires a delivery address"
            )
        threshold = settings.free_delivery_from
        if threshold is not None and subtotal >= threshold:
            return _ZERO
        return settings.courier_rate

    def _build_order_out(
        self,
        order: Order,
        item_models: list[OrderItem],
    ) -> OrderOut:
        """Assemble the response DTO from the persisted order + lines.

        Built explicitly (rather than ``from_attributes`` on ``Order``) because
        the ORM ``Order`` intentionally has no ``items`` relationship.

        Args:
            order: The persisted order.
            item_models: The persisted order lines.

        Returns:
            OrderOut: The response projection.
        """
        return OrderOut(
            number=order.number,
            status=order.status,
            payment_status=order.payment_status,
            email=order.email,
            phone=order.phone,
            subtotal=order.subtotal,
            discount_total=order.discount_total,
            delivery_cost=order.delivery_cost,
            total=order.total,
            delivery_type=order.delivery_type,
            delivery_address_id=order.delivery_address_id,
            delivery_name=order.delivery_name,
            delivery_city=order.delivery_city,
            delivery_street=order.delivery_street,
            delivery_zip=order.delivery_zip,
            payment_method=order.payment_method,
            created_at=order.created_at,
            items=[OrderItemOut.model_validate(item) for item in item_models],
        )

    async def _send_confirmation(
        self,
        number: str,
        email: str,
        total: Decimal,
    ) -> None:
        """Fire the post-commit order-confirmation side effect (COD, §9.8).

        Delegates to :func:`app.core.email.send_order_confirmation`, whose
        backend is chosen by ``settings.email_backend``. Kept out of the
        transaction and defensively wrapped: a delivery failure is logged but
        never propagated, so mail trouble can't invalidate a committed order.

        Args:
            number: The created order number.
            email: The recipient contact email.
            total: The order grand total (shown in the email body).
        """
        try:
            await send_order_confirmation(
                to=email,
                order_number=number,
                total=total,
            )
        except Exception:  # noqa: BLE001 — side effect must not break checkout
            logger.exception(
                "order %s created for %s: confirmation email failed",
                number,
                email,
            )
