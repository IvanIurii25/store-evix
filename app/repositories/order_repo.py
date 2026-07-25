"""Async data-access layer for the checkout + order domain (stage B6, §9 / §8).

Only SQL / query construction lives here — no business rules (those belong to
:class:`~app.services.checkout_service.CheckoutService` and
:class:`~app.services.order_service.OrderService`). All statements are async
SQLAlchemy 2.0 bound to one session.

Two race-critical primitives live here:

* :meth:`OrderRepository.next_order_number` draws a value from the DB sequence
  ``order_number_seq`` and formats it as ``YYYYMMDD-000042`` — concurrency-safe
  because ``nextval`` never hands the same value to two callers (§9.7).
* :meth:`OrderRepository.decrement_stock` runs a conditional
  ``UPDATE ... WHERE qty >= :n`` and reports the affected row count, so two
  buyers racing for the last unit cannot both succeed (§9.6).
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.cart import Cart, CartItem
from app.models.catalog import Product, ProductTranslation
from app.models.order import Order, OrderItem, OrderStatusHistory

# Status of a live, still-editable cart (§2.3): only ``draft`` carts convert.
ACTIVE_CART_STATUS: str = "draft"

# Cart status after a successful checkout (§9.6).
CONVERTED_CART_STATUS: str = "converted"

# Fallback language for a line's snapshot name (checkout is language-agnostic;
# §2.6 default is ``ro``).
NAME_LANG: str = "ro"

# Zero-padding width of the sequence portion of an order number (§9.7).
_NUMBER_PAD: int = 6


class OrderRepository:
    """Read/write queries for checkout + order entities, bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used by every query method.

        Args:
            session: Active async session (request-scoped or test-scoped).
        """
        self.session = session

    # ------------------------------------------------------------------ #
    # Cart resolution (checkout reads the caller's cart)
    # ------------------------------------------------------------------ #
    async def get_active_cart(
        self,
        user_id: int | None,
        session_token: UUID | None,
    ) -> Cart | None:
        """Return the caller's active (``draft``) cart, if any (§9.1).

        Args:
            user_id: Owning user id, or ``None`` for a guest.
            session_token: Guest cookie token, or ``None`` for a user.

        Returns:
            Cart | None: The active cart, or ``None`` when none exists.
        """
        if user_id is not None:
            stmt = select(Cart).where(
                Cart.user_id == user_id,
                Cart.status == ACTIVE_CART_STATUS,
            )
        elif session_token is not None:
            stmt = select(Cart).where(
                Cart.session_token == session_token,
                Cart.status == ACTIVE_CART_STATUS,
            )
        else:
            return None
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_cart_lines(
        self,
        cart_id: int,
        lang: str,
    ) -> list[tuple[CartItem, Product, str | None]]:
        """Return cart lines joined to product + localized name (single query).

        The inner join to ``product`` drops lines whose product row is gone. The
        snapshot name is resolved language-aware via two LEFT joins to
        ``product_translation`` — the requested ``lang`` and the default
        :data:`NAME_LANG` — coalesced so the value is
        ``requested-lang → default-lang → None`` (the service then falls back to
        ``product.code`` when ``None``). This is what the order's
        ``name_snapshot`` captures, so the snapshot is in the order's language.
        When ``lang == NAME_LANG`` the two joins are harmless.

        Args:
            cart_id: Cart id.
            lang: Requested snapshot language (validated by the caller).

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

    async def mark_cart_converted(self, cart_id: int) -> None:
        """Flip a cart to ``converted`` after its order is created (§9.6).

        Args:
            cart_id: Cart id.
        """
        stmt = (
            update(Cart).where(Cart.id == cart_id).values(status=CONVERTED_CART_STATUS)
        )
        await self.session.execute(stmt)

    # ------------------------------------------------------------------ #
    # Order number (race-safe via sequence)
    # ------------------------------------------------------------------ #
    async def next_order_number(self) -> str:
        """Return a fresh, human-readable order number ``YYYYMMDD-000042`` (§9.7).

        Draws the counter from ``order_number_seq`` (``nextval`` is atomic — no
        two callers ever get the same value) and prefixes it with today's UTC
        date. Formatting is done in Python for clarity/testability.

        Returns:
            str: The unique order number.
        """
        seq = await self.session.scalar(text("SELECT nextval('order_number_seq')"))
        today = datetime.now(UTC).strftime("%Y%m%d")
        return f"{today}-{int(seq):0{_NUMBER_PAD}d}"

    # ------------------------------------------------------------------ #
    # Stock decrement (race-safe conditional UPDATE)
    # ------------------------------------------------------------------ #
    async def decrement_stock(self, product_id: int, qty: int) -> bool:
        """Atomically decrement product stock, guarding against oversell (§9.6).

        Runs ``UPDATE product SET qty = qty - :n WHERE id = :id AND qty >= :n``
        and reports whether a row was affected. A ``False`` result means another
        concurrent order already took the stock — the caller must roll back with
        an ``out_of_stock`` error.

        Args:
            product_id: Catalog product id to decrement.
            qty: Quantity to remove from stock.

        Returns:
            bool: ``True`` if stock was decremented, ``False`` on insufficient
                stock (rowcount 0).
        """
        stmt = (
            update(Product)
            .where(Product.id == product_id, Product.qty >= qty)
            .values(qty=Product.qty - qty)
        )
        result = await self.session.execute(stmt)
        return bool(result.rowcount)

    # ------------------------------------------------------------------ #
    # Stock increment (return on cancel)
    # ------------------------------------------------------------------ #
    async def increment_stock(self, product_id: int, qty: int) -> bool:
        """Atomically return stock to a product and report a 0 → >0 crossing (§8).

        Mirror of :meth:`decrement_stock` for the cancel path: runs
        ``UPDATE product SET qty = qty + :n WHERE id = :id`` (no ``qty >= :n``
        guard — a return is always valid). Before the update it reads the current
        stock so it can report whether the product was at zero, i.e. this return
        crosses ``0 → >0`` and should trigger a restock notification.

        Args:
            product_id: Catalog product id to credit.
            qty: Quantity to add back to stock.

        Returns:
            bool: ``True`` if the product was at ``qty == 0`` before the increment
                (so it just crossed back into stock), else ``False``. A missing
                product row (no rowcount) also yields ``False``.
        """
        before = await self.session.scalar(
            select(Product.qty).where(Product.id == product_id)
        )
        stmt = (
            update(Product)
            .where(Product.id == product_id)
            .values(qty=Product.qty + qty)
        )
        result = await self.session.execute(stmt)
        return bool(result.rowcount) and before == 0

    async def get_items_for_order(self, order_id: int) -> list[OrderItem]:
        """Return one order's lines (ordered by id) for the stock-return effect.

        Args:
            order_id: Order id.

        Returns:
            list[OrderItem]: The order's lines (empty if none).
        """
        stmt = (
            select(OrderItem)
            .where(OrderItem.order_id == order_id)
            .order_by(OrderItem.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # ------------------------------------------------------------------ #
    # Order persistence
    # ------------------------------------------------------------------ #
    def add_order(
        self,
        *,
        number: str,
        user_id: int | None,
        email: str,
        phone: str,
        subtotal: Decimal,
        discount_total: Decimal,
        delivery_cost: Decimal,
        total: Decimal,
        delivery_type: str,
        delivery_address_id: int | None,
        delivery_name: str | None = None,
        delivery_city: str | None = None,
        delivery_street: str | None = None,
        delivery_zip: str | None = None,
    ) -> Order:
        """Instantiate and stage a new ``new`` / ``pending`` COD order (§9.6).

        Args:
            number: Pre-generated order number.
            user_id: Owning user id, or ``None`` for a guest order.
            email: Contact email.
            phone: Contact phone.
            subtotal: Sum of line snapshots.
            discount_total: Discount applied (0 in v1 — no promo).
            delivery_cost: Delivery charge.
            total: ``subtotal - discount_total + delivery_cost``.
            delivery_type: ``pickup`` or ``courier``.
            delivery_address_id: Saved address id (courier), else ``None``.
            delivery_name: Courier address snapshot — recipient, else ``None``.
            delivery_city: Courier address snapshot — city, else ``None``.
            delivery_street: Courier address snapshot — street, else ``None``.
            delivery_zip: Courier address snapshot — postal code, else ``None``.

        Returns:
            Order: The staged (not yet flushed) order.
        """
        order = Order(
            number=number,
            user_id=user_id,
            email=email,
            phone=phone,
            status="new",
            payment_status="pending",
            subtotal=subtotal,
            discount_total=discount_total,
            delivery_cost=delivery_cost,
            total=total,
            delivery_type=delivery_type,
            delivery_address_id=delivery_address_id,
            delivery_name=delivery_name,
            delivery_city=delivery_city,
            delivery_street=delivery_street,
            delivery_zip=delivery_zip,
            payment_method="cod",
        )
        self.session.add(order)
        return order

    def add_order_item(
        self,
        *,
        order_id: int,
        product_id: int | None,
        name_snapshot: str,
        price_snapshot: Decimal,
        qty: int,
    ) -> OrderItem:
        """Stage one order line with its name/price snapshot (§2.4).

        Args:
            order_id: Parent order id.
            product_id: Source product id (``SET NULL`` on delete, §11).
            name_snapshot: Product name at order time.
            price_snapshot: Product price at order time.
            qty: Quantity ordered.

        Returns:
            OrderItem: The staged (not yet flushed) line.
        """
        item = OrderItem(
            order_id=order_id,
            product_id=product_id,
            name_snapshot=name_snapshot,
            price_snapshot=price_snapshot,
            qty=qty,
        )
        self.session.add(item)
        return item

    def add_status_history(
        self,
        *,
        order_id: int,
        from_status: str,
        to_status: str,
        changed_by: str,
    ) -> OrderStatusHistory:
        """Stage an audit row for an order status transition (§8).

        Args:
            order_id: The order whose status changed.
            from_status: Prior status (or ``""`` for the initial record).
            to_status: New status.
            changed_by: Actor id (``system`` / ``admin`` / user id).

        Returns:
            OrderStatusHistory: The staged (not yet flushed) audit row.
        """
        record = OrderStatusHistory(
            order_id=order_id,
            from_status=from_status,
            to_status=to_status,
            changed_at=datetime.now(UTC),
            changed_by=changed_by,
        )
        self.session.add(record)
        return record

    # ------------------------------------------------------------------ #
    # Order reads
    #
    # The ORM ``Order`` intentionally carries no ``items`` relationship, so items
    # are fetched with an explicit second query (batched by order id) — the
    # service pairs each order with its lines. This keeps model definitions
    # untouched while still avoiding a per-order N+1 on the list endpoint.
    # ------------------------------------------------------------------ #
    async def get_order_by_number(self, number: str) -> Order | None:
        """Load an order by its human-readable number.

        Args:
            number: Order number.

        Returns:
            Order | None: The order, or ``None``.
        """
        stmt = select(Order).where(Order.number == number)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_order_by_id(self, order_id: int) -> Order | None:
        """Load an order by its primary key.

        Args:
            order_id: Order primary key.

        Returns:
            Order | None: The order, or ``None``.
        """
        stmt = select(Order).where(Order.id == order_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_orders_for_user(self, user_id: int) -> list[Order]:
        """Return a user's orders (newest first).

        Args:
            user_id: Owning user id.

        Returns:
            list[Order]: The user's orders.
        """
        stmt = (
            select(Order)
            .where(Order.user_id == user_id)
            .order_by(Order.created_at.desc(), Order.id.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_items_for_orders(
        self,
        order_ids: list[int],
    ) -> dict[int, list[OrderItem]]:
        """Fetch all order lines for the given orders in one query (no N+1).

        Args:
            order_ids: Order ids to load lines for.

        Returns:
            dict[int, list[OrderItem]]: ``order_id`` → its lines (ordered by id).
        """
        grouped: dict[int, list[OrderItem]] = {oid: [] for oid in order_ids}
        if not order_ids:
            return grouped
        stmt = (
            select(OrderItem)
            .where(OrderItem.order_id.in_(order_ids))
            .order_by(OrderItem.id)
        )
        for item in (await self.session.execute(stmt)).scalars().all():
            grouped[item.order_id].append(item)
        return grouped

    async def count_status_history(self, order_id: int) -> int:
        """Return the number of status-history rows for an order (test helper).

        Args:
            order_id: Order id.

        Returns:
            int: Count of ``order_status_history`` rows.
        """
        stmt = select(func.count()).where(OrderStatusHistory.order_id == order_id)
        return int((await self.session.scalar(stmt)) or 0)
