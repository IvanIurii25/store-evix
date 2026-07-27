"""Checkout business logic: quote + atomic order creation (stage B6, §9).

Holds the rules the router and repository must not: loading the caller's cart,
server-side stock validation, delivery / discount / total computation, and the
single-transaction creation of an order with snapshotted lines, race-safe stock
decrement, cart conversion and initial status history (§9).

Discounts: an optional promo code (feature A1, §2.5) is validated by
:class:`~app.services.promo_service.PromoService` and subtracted from the
subtotal; with no code ``discount_total`` is 0. Tax: the ``order`` table has no
tax column, so tax is NOT persisted here (deferred, §9.5).

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
from app.models.cart import Cart
from app.models.order import Order, OrderItem
from app.models.user import Address
from app.repositories.catalog_repo import CatalogRepository
from app.repositories.order_repo import NAME_LANG, OrderRepository
from app.repositories.user_repo import UserRepository
from app.schemas.order import DeliveryAddressIn, OrderItemOut, OrderOut, QuoteOut
from app.services.payment.payment_service import PaymentService
from app.services.promo_service import PromoService
from app.tasks.order_email import send_order_confirmation_email

logger = logging.getLogger(__name__)

# Delivery types (§9.4). ``courier`` requires an address; ``pickup`` is free.
PICKUP: str = "pickup"
COURIER: str = "courier"

# Payment methods. ``cod`` is the default (unchanged path); ``card`` triggers the
# maib payment initiation after the order is persisted.
_COD: str = "cod"
_CARD: str = "card"

# Zero money constant (baseline discount / free delivery).
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


class ProductNotFoundError(CheckoutError):
    """No active product matches the requested id (one-click buy, A2).

    Raised by :meth:`CheckoutService.quick_buy` when the product is missing or
    inactive. Mapped to HTTP 404 with code ``product_not_found``. Carries the
    offending ``product_id`` for the error ``details``.
    """

    def __init__(self, product_id: int) -> None:
        self.product_id = product_id
        super().__init__(f"Product {product_id} not found or unavailable")


class _PricedLine:
    """A resolved cart line ready to price / persist (internal to checkout)."""

    __slots__ = ("product_id", "variant_id", "name", "price", "qty")

    def __init__(
        self,
        product_id: int,
        name: str,
        price: Decimal,
        qty: int,
        variant_id: int | None = None,
    ) -> None:
        self.product_id = product_id
        self.variant_id = variant_id
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
        self.promo_service = PromoService(session)
        self.catalog = CatalogRepository(session)

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
        promo_code: str | None = None,
        lang: str = NAME_LANG,
    ) -> QuoteOut:
        """Compute the checkout totals without creating an order (§9, idempotent).

        Args:
            user_id: Owning user id, or ``None`` for a guest.
            session_token: Guest cookie token, or ``None`` for a user.
            delivery_type: ``pickup`` or ``courier``.
            delivery_address_id: Saved address id (courier; logged-in users).
            delivery_address: Inline courier address (guest or user).
            promo_code: Optional coupon code applied to the subtotal.
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
            PromoError: If ``promo_code`` is supplied but not usable.
        """
        _cart, lines = await self._load_cart_lines(user_id, session_token, lang)
        # Validate ownership even though the delivery cost is owner-independent:
        # a foreign id must not even be quotable.
        await self._resolve_delivery_snapshot(
            user_id, delivery_type, delivery_address_id, delivery_address
        )
        discount_total = await self._resolve_discount(lines, promo_code)
        return self._quote_from_lines(
            lines,
            delivery_type,
            delivery_address_id,
            delivery_address,
            discount_total=discount_total,
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
        promo_code: str | None = None,
        payment_method: str = "cod",
        client_ip: str | None = None,
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
            promo_code: Optional coupon code; re-validated here (the client's
                quoted discount is never trusted) and snapshotted onto the order.
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
            PromoError: If ``promo_code`` is supplied but not usable.
        """
        cart, lines = await self._load_cart_lines(user_id, session_token, lang)
        (
            snap_name,
            snap_city,
            snap_street,
            snap_zip,
        ) = await self._resolve_delivery_snapshot(
            user_id, delivery_type, delivery_address_id, delivery_address
        )
        # Re-validate the coupon server-side: the discount is recomputed, never
        # taken from the client. A promo that expired between quote and checkout
        # raises here and no order is created. ``lock=True`` serializes concurrent
        # redemptions of a usage-limited code so the limit cannot be overshot.
        discount_total = await self._resolve_discount(lines, promo_code, lock=True)
        totals = self._quote_from_lines(
            lines,
            delivery_type,
            delivery_address_id,
            delivery_address,
            discount_total=discount_total,
        )
        applied_code = promo_code if discount_total > _ZERO else None

        order_out = await self._persist_order(
            lines=lines,
            user_id=user_id,
            email=email,
            phone=phone,
            totals=totals,
            delivery_type=delivery_type,
            delivery_address_id=delivery_address_id,
            snapshot=(snap_name, snap_city, snap_street, snap_zip),
            promo_code=applied_code,
            cart_id=cart.id,
            payment_method=payment_method,
        )
        if payment_method == _CARD:
            order_out.pay_url = await self._initiate_card_payment(
                order_out.number, client_ip
            )
        return order_out

    # ------------------------------------------------------------------ #
    # Quick buy (one-click COD, feature A2 — no cart, single product)
    # ------------------------------------------------------------------ #
    async def quick_buy(
        self,
        *,
        product_id: int,
        phone: str,
        name: str | None = None,
        email: str | None = None,
        qty: int = 1,
        lang: str = NAME_LANG,
        variant_id: int | None = None,
    ) -> OrderOut:
        """Create a one-product COD order without a cart or full checkout (A2).

        Resolves and stock-checks a single active product, then reuses the same
        atomic persistence core as :meth:`checkout` (:meth:`_persist_order`):
        race-safe stock decrement, snapshotted line, order number, status
        history and one commit, with the post-commit confirmation email. Always
        free pickup — no delivery address and no discount. ``email`` is optional;
        when omitted a deterministic placeholder is derived from the phone (the
        ``order.email`` column is NOT NULL). ``name`` is snapshotted onto
        ``delivery_name``.

        Args:
            product_id: Product to buy.
            phone: Contact phone (required — the operator calls back).
            name: Optional customer name (snapshotted as ``delivery_name``).
            email: Optional contact email; a placeholder is derived from the
                phone when omitted.
            qty: Quantity to buy (defaults to 1).
            lang: Requested snapshot language for the line name.

        Returns:
            OrderOut: The created order with its single line.

        Raises:
            ProductNotFoundError: If no active product matches ``product_id``.
            OutOfStockError: If stock is insufficient (at read or at decrement).
        """
        norm_lang = normalize_lang(lang)
        resolved = await self.repo.get_product_with_name(product_id, norm_lang)
        if resolved is None:
            raise ProductNotFoundError(product_id)
        product, name_snapshot = resolved

        # Resolve the variant for variable products (required); simple products
        # forbid one. Price/stock/label then come from the chosen variant.
        variant = None
        if product.has_variants:
            if variant_id is None:
                raise ProductNotFoundError(product_id)
            variant = await self.catalog.get_variant(variant_id)
            if (
                variant is None
                or variant.product_id != product.id
                or not variant.is_active
            ):
                raise ProductNotFoundError(product_id)
        elif variant_id is not None:
            raise ProductNotFoundError(product_id)

        available = variant.qty if variant is not None else product.qty
        if qty > available:
            raise OutOfStockError(product.id)

        base_name = name_snapshot if name_snapshot is not None else product.code
        if variant is not None:
            labels = await self.catalog.get_variant_option_labels(
                [variant.id], norm_lang
            )
            label = labels.get(variant.id)
            if label:
                base_name = f"{base_name} ({label})"
        line = _PricedLine(
            product_id=product.id,
            variant_id=variant.id if variant is not None else None,
            name=base_name,
            price=variant.price if variant is not None else product.price,
            qty=qty,
        )
        totals = self._quote_from_lines([line], PICKUP, None)
        contact_email = email if email else self._placeholder_email(phone)
        snapshot: _DeliverySnapshot = (name, None, None, None)
        return await self._persist_order(
            lines=[line],
            user_id=None,
            email=contact_email,
            phone=phone,
            totals=totals,
            delivery_type=PICKUP,
            delivery_address_id=None,
            snapshot=snapshot,
            promo_code=None,
            cart_id=None,
        )

    @staticmethod
    def _placeholder_email(phone: str) -> str:
        """Derive a NOT-NULL-satisfying placeholder email from a phone (A2).

        The ``order.email`` column is ``NOT NULL``; a phone-only quick buy has no
        email, so a deterministic ``<digits>@quick.evix.md`` address is used. It
        is a valid local-part (digits only) and clearly marks the order as
        one-click (the operator has the real phone).

        Args:
            phone: The contact phone (any format).

        Returns:
            str: The placeholder email address.
        """
        digits = "".join(ch for ch in phone if ch.isdigit()) or "0"
        return f"{digits}@quick.evix.md"

    # ------------------------------------------------------------------ #
    # Atomic order persistence (shared by checkout + quick_buy)
    # ------------------------------------------------------------------ #
    async def _persist_order(
        self,
        *,
        lines: list["_PricedLine"],
        user_id: int | None,
        email: str,
        phone: str,
        totals: QuoteOut,
        delivery_type: str,
        delivery_address_id: int | None,
        snapshot: _DeliverySnapshot,
        promo_code: str | None,
        cart_id: int | None,
        payment_method: str = "cod",
    ) -> OrderOut:
        """Create the order + lines in one transaction (§9.6, shared core).

        Extracted from :meth:`checkout` so :meth:`quick_buy` reuses the exact
        atomic path without duplicating it: generate a race-safe number, insert
        the order + snapshotted lines, decrement stock with the conditional
        UPDATE (oversell → rollback + :class:`OutOfStockError`), optionally
        convert the cart, and write the initial status history. The confirmation
        email fires only after ``commit`` (§9.8). Behaviour for the cart path is
        unchanged; ``cart_id=None`` simply skips cart conversion (quick buy).

        Args:
            lines: Resolved, stock-checked lines to persist.
            user_id: Owning user id, or ``None`` for a guest order.
            email: Contact email stored on the order (never ``None``).
            phone: Contact phone stored on the order.
            totals: Pre-computed subtotal / discount / delivery / total.
            delivery_type: ``pickup`` or ``courier``.
            delivery_address_id: Saved address id (courier), else ``None``.
            snapshot: ``(name, city, street, zip)`` delivery snapshot.
            promo_code: Redeemed coupon snapshot, else ``None``.
            cart_id: Cart to mark converted, or ``None`` to skip (quick buy).

        Returns:
            OrderOut: The created order with its lines.

        Raises:
            OutOfStockError: If any line's stock is insufficient at commit time.
        """
        snap_name, snap_city, snap_street, snap_zip = snapshot
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
            promo_code=promo_code,
            payment_method=payment_method,
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
                    variant_id=line.variant_id,
                    name_snapshot=line.name,
                    price_snapshot=line.price,
                    qty=line.qty,
                )
            )

        if cart_id is not None:
            await self.repo.mark_cart_converted(cart_id)
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

        # Decrement stock LAST, immediately before commit, so the product row
        # lock the conditional UPDATE takes is held for the shortest possible
        # window (only across the commit, not across the cart/history writes) —
        # this keeps checkouts of the same product contending minimally during a
        # flash sale. Rollback on oversell correctly undoes the earlier cart and
        # status-history writes too.
        for line in lines:
            decremented = await self.repo.decrement_stock(
                line.product_id, line.qty, line.variant_id
            )
            if not decremented:
                await self.session.rollback()
                raise OutOfStockError(line.product_id)

        await self.session.commit()
        await self._send_confirmation(order.number, email, order.total)
        return self._build_order_out(order, item_models)

    # ------------------------------------------------------------------ #
    # Card payment initiation (maib) — only when payment_method == card
    # ------------------------------------------------------------------ #
    async def _initiate_card_payment(
        self,
        order_number: str,
        client_ip: str | None,
    ) -> str:
        """Initiate the maib payment for a just-created card order → payUrl.

        Loads the committed order fresh (it was created in :meth:`_persist_order`),
        delegates to :class:`~app.services.payment.payment_service.PaymentService`
        to create the ``payment`` row and call maib ``/pay``, and returns the
        ``payUrl``. Bound to the same session so the payment row commits alongside.

        Args:
            order_number: The created order's number.
            client_ip: The payer's IP (``0.0.0.0`` fallback when unavailable).

        Returns:
            str: The maib ``payUrl`` for the storefront redirect.
        """
        order = await self.repo.get_order_by_number(order_number)
        payment_service = PaymentService(self.session)
        return await payment_service.initiate_card_payment(
            order,
            client_ip=client_ip or "0.0.0.0",
            lang=NAME_LANG,
        )

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

        norm_lang = normalize_lang(lang)
        lines = await self.repo.list_cart_lines(cart.id, norm_lang)
        variant_ids = [ci.variant_id for ci, _p, _n in lines if ci.variant_id]
        variants = await self.catalog.get_variants_by_ids(variant_ids)
        labels = await self.catalog.get_variant_option_labels(variant_ids, norm_lang)

        priced: list[_PricedLine] = []
        for cart_item, product, name in lines:
            if not product.is_active:
                continue
            # Variable-product line: price/stock come from the variant. A stale
            # line (variant gone or deactivated) is skipped like an inactive
            # product, so it never blocks checkout of the rest of the cart.
            variant = variants.get(cart_item.variant_id) if cart_item.variant_id else None
            if cart_item.variant_id is not None and (
                variant is None or not variant.is_active
            ):
                continue
            unit_price = variant.price if variant is not None else product.price
            available = variant.qty if variant is not None else product.qty
            if cart_item.qty > available:
                raise OutOfStockError(product.id)
            base_name = name if name is not None else product.code
            label = labels.get(cart_item.variant_id) if cart_item.variant_id else None
            priced.append(
                _PricedLine(
                    product_id=product.id,
                    variant_id=cart_item.variant_id,
                    name=f"{base_name} ({label})" if label else base_name,
                    price=unit_price,
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

    async def _resolve_discount(
        self,
        lines: list[_PricedLine],
        promo_code: str | None,
        *,
        lock: bool = False,
    ) -> Decimal:
        """Validate the coupon (if any) and return the discount for the subtotal.

        Single source of truth for "how much does this code take off", reused by
        both :meth:`quote` and :meth:`checkout` so they agree. No code → zero.

        Args:
            lines: Resolved, stock-checked cart lines (source of the subtotal).
            promo_code: Optional coupon code.
            lock: Passed to the promo service to lock a usage-limited code for the
                duration of the checkout transaction (``True`` from
                :meth:`checkout`, ``False`` from :meth:`quote`).

        Returns:
            Decimal: The discount amount (``0`` when no code is supplied).

        Raises:
            PromoError: If ``promo_code`` is supplied but not usable (unknown,
                expired, below min-order, or usage-limited).
        """
        if not promo_code:
            return _ZERO
        subtotal = sum((line.line_total for line in lines), _ZERO)
        _promo, discount = await self.promo_service.validate_and_compute(
            promo_code, subtotal, lock=lock
        )
        return discount

    def _quote_from_lines(
        self,
        lines: list[_PricedLine],
        delivery_type: str,
        delivery_address_id: int | None,
        delivery_address: DeliveryAddressIn | None = None,
        *,
        discount_total: Decimal = _ZERO,
    ) -> QuoteOut:
        """Compute totals from resolved lines + delivery choice (§9.3–9.5).

        Args:
            lines: Resolved, stock-checked cart lines.
            delivery_type: ``pickup`` or ``courier``.
            delivery_address_id: Saved address id (courier; logged-in users).
            delivery_address: Inline courier address (guest or user).
            discount_total: Promo discount to subtract from the subtotal (already
                validated + clamped by :meth:`_resolve_discount`; default zero).

        Returns:
            QuoteOut: The computed breakdown.

        Raises:
            DeliveryAddressRequiredError: If courier is chosen without an address.
        """
        subtotal = sum((line.line_total for line in lines), _ZERO)
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
        """Dispatch the post-commit order-confirmation side effect (COD, §9.8).

        The email is no longer sent inline: this enqueues the durable Celery task
        :func:`app.tasks.order_email.send_order_confirmation_email` (name
        ``order.confirm_email``), moving the ~1s SMTP send off the request hot
        path onto the worker. ``total`` is passed as a string because Celery
        serializes args as JSON over the broker and ``Decimal`` is not
        JSON-serializable; the worker reconstructs the ``Decimal``. Kept out of
        the transaction and defensively wrapped: an enqueue failure (e.g. broker
        down) is logged but never propagated, so it can't invalidate a committed
        order.

        Args:
            number: The created order number.
            email: The recipient contact email.
            total: The order grand total (shown in the email body).
        """
        try:
            send_order_confirmation_email.delay(
                to=email,
                order_number=number,
                total_str=str(total),
            )
        except Exception:  # noqa: BLE001 — side effect must not break checkout
            logger.exception(
                "order %s created for %s: confirmation email enqueue failed",
                number,
                email,
            )
