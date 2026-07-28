"""Promo-code business logic: validation + discount computation + admin CRUD.

Feature A1 (§2.5). Two concerns share one service bound to a session:

* Storefront: :meth:`PromoService.validate_and_compute` — the single source of
  truth for "is this code usable right now, and what does it take off". Quote and
  checkout both call it so they can never disagree; checkout re-validates rather
  than trusting the client. It raises typed :class:`PromoError` subclasses (each
  carries a machine ``code``) that the router maps to 400/404/409.
* Back office: create / list / get / update / delete coupons.

Discount rules (§2.5):

* ``percent`` → ``subtotal * value / 100``; ``fixed`` → ``min(value, subtotal)``.
* Result is quantized to whole MDL (0 decimals, ``ROUND_HALF_UP``) and never
  exceeds the subtotal, so ``total`` can never go negative from a discount. The
  store prices in whole MDL, so the discount is rounded to whole MDL too — the
  displayed discount then equals the charged discount (BUG-02).

The service owns no HTTP knowledge and no SQL (that is :class:`PromoRepository`).
"""

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.models.promo import DISCOUNT_TYPES, PromoCode
from app.repositories.promo_repo import PromoRepository
from app.schemas.admin_promo import PromoCreate, PromoUpdate

# Money precision: whole MDL, half-up rounding (§2.6). The store prices in whole
# MDL, so discounts quantize to whole MDL as well (BUG-02: display == charged).
_UNIT: Decimal = Decimal("1")
_ZERO: Decimal = Decimal("0")
_HUNDRED: Decimal = Decimal("100")

# Discount-type literals (kept in sync with the model's allowed set).
_PERCENT: str = "percent"
_FIXED: str = "fixed"


class PromoError(DomainError):
    """Base class for promo domain errors (rendered via the unified envelope).

    Each leaf declares ``status_code`` + ``code``; the registered
    :class:`~app.core.errors.DomainError` handler emits the
    ``{error:{code,message}}`` envelope with the leaf's status.
    """

    code: str = "promo_error"


class PromoInvalidError(PromoError):
    """The code is unknown or disabled (mapped to 404)."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "promo_invalid"


class PromoExpiredError(PromoError):
    """Now is outside the code's validity window (mapped to 400)."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "promo_expired"


class PromoMinOrderError(PromoError):
    """The subtotal is below the code's minimum order total (mapped to 400)."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "promo_min_order"


class PromoUsageLimitError(PromoError):
    """The code's redemption limit is exhausted (mapped to 409)."""

    status_code = status.HTTP_409_CONFLICT
    code = "promo_usage_limit"


class PromoNotFoundError(PromoError):
    """The referenced coupon does not exist (admin path, mapped to 404)."""

    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class PromoConflictError(PromoError):
    """An admin write violates an invariant (duplicate code, mapped to 409)."""

    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class PromoValidationError(PromoError):
    """An admin write carries an invalid value (mapped to 422)."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_error"


class PromoService:
    """Promo use-cases (storefront validation + admin CRUD) bound to a session."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to a session and its repository.

        Args:
            session: Active async session (request-scoped or test-scoped).
        """
        self.session = session
        self.repo = PromoRepository(session)

    # ------------------------------------------------------------------ #
    # Storefront: validate + compute discount
    # ------------------------------------------------------------------ #
    async def validate_and_compute(
        self,
        code: str,
        subtotal: Decimal,
        *,
        now: datetime | None = None,
        lock: bool = False,
    ) -> tuple[PromoCode, Decimal]:
        """Validate a code against a subtotal and return ``(promo, discount)``.

        Called by both quote and checkout so they compute an identical discount;
        checkout re-runs it at order time (the client's number is never trusted).

        Args:
            code: The coupon code as typed by the shopper.
            subtotal: The cart subtotal the discount applies to.
            now: Reference instant for the validity window (defaults to UTC now;
                injectable for deterministic tests).
            lock: When ``True`` (checkout) and the code is usage-limited, take a
                ``FOR UPDATE`` lock on the promo row before counting redemptions
                so concurrent checkouts of the same code cannot overshoot the
                limit. ``quote`` leaves it ``False`` (read-only preview, no lock).

        Returns:
            tuple[PromoCode, Decimal]: The validated promo row and the discount
                amount (quantized, ``0 <= discount <= subtotal``).

        Raises:
            PromoInvalidError: If the code is unknown or disabled.
            PromoExpiredError: If ``now`` is outside the validity window.
            PromoMinOrderError: If ``subtotal`` is below ``min_order_total``.
            PromoUsageLimitError: If the redemption limit is exhausted.
        """
        moment = now if now is not None else datetime.now(UTC)
        promo = await self.repo.get_by_code(code)
        if promo is None or not promo.is_active:
            raise PromoInvalidError(f"Unknown or inactive promo code: {code}")

        if not self._within_window(promo, moment):
            raise PromoExpiredError("Promo code is not valid at this time")

        if promo.min_order_total is not None and subtotal < promo.min_order_total:
            raise PromoMinOrderError(
                f"Subtotal below the minimum for promo code {code}"
            )

        if promo.usage_limit is not None:
            # Serialize redeemers of this code before counting (checkout only);
            # the lock is held until the checkout commits its order.
            if lock:
                await self.repo.lock_by_code(promo.code)
            used = await self.repo.count_orders_using(promo.code)
            if used >= promo.usage_limit:
                raise PromoUsageLimitError("Promo code usage limit reached")

        return promo, self._compute_discount(promo, subtotal)

    @staticmethod
    def _within_window(promo: PromoCode, moment: datetime) -> bool:
        """Return whether ``moment`` falls inside the promo's validity window.

        ``None`` boundaries are treated as open-ended (defensive: the columns are
        non-null in the schema, but the rule is expressed independently of that).

        Args:
            promo: The promo row.
            moment: The instant being tested (timezone-aware UTC).

        Returns:
            bool: ``True`` if ``active_from <= moment <= active_to``.
        """
        if promo.active_from is not None and moment < promo.active_from:
            return False
        return not (promo.active_to is not None and moment > promo.active_to)

    @staticmethod
    def _compute_discount(promo: PromoCode, subtotal: Decimal) -> Decimal:
        """Compute the discount for a promo against a subtotal (§2.5).

        ``percent`` → ``subtotal * value / 100``; ``fixed`` → ``value``. The
        result is quantized to whole MDL (0 decimals, ``ROUND_HALF_UP``) and
        clamped to ``[0, subtotal]`` so the order total can never be dragged
        negative. Whole-MDL rounding keeps the displayed discount equal to the
        charged discount, consistent with the store's whole-MDL prices (BUG-02).

        Args:
            promo: The validated promo row.
            subtotal: The cart subtotal.

        Returns:
            Decimal: The discount amount (0..subtotal, whole MDL).
        """
        if promo.discount_type == _PERCENT:
            raw = subtotal * promo.discount_value / _HUNDRED
        else:
            raw = promo.discount_value
        discount = raw.quantize(_UNIT, rounding=ROUND_HALF_UP)
        if discount < _ZERO:
            return _ZERO
        return min(discount, subtotal)

    # ------------------------------------------------------------------ #
    # Back office: CRUD
    # ------------------------------------------------------------------ #
    async def list_promos(self) -> list[PromoCode]:
        """Return every promo code (newest first) for the roster."""
        return await self.repo.list_all()

    async def get_promo(self, promo_id: int) -> PromoCode:
        """Return one promo by id or raise.

        Args:
            promo_id: The promo primary key.

        Returns:
            PromoCode: The row.

        Raises:
            PromoNotFoundError: If no such promo exists.
        """
        promo = await self.repo.get_by_id(promo_id)
        if promo is None:
            raise PromoNotFoundError(f"Promo code not found: {promo_id}")
        return promo

    async def create_promo(self, payload: PromoCreate) -> PromoCode:
        """Create a new coupon.

        Args:
            payload: The validated create body.

        Returns:
            PromoCode: The persisted promo.

        Raises:
            PromoValidationError: If the discount definition is inconsistent.
            PromoConflictError: If the code already exists.
        """
        self._validate_definition(
            discount_type=payload.discount_type,
            discount_value=payload.discount_value,
            active_from=payload.active_from,
            active_to=payload.active_to,
        )
        if await self.repo.get_by_code(payload.code) is not None:
            raise PromoConflictError(f"Promo code already exists: {payload.code}")

        promo = PromoCode(
            code=payload.code,
            discount_type=payload.discount_type,
            discount_value=payload.discount_value,
            active_from=payload.active_from,
            active_to=payload.active_to,
            min_order_total=payload.min_order_total,
            usage_limit=payload.usage_limit,
            is_active=payload.is_active,
        )
        self.repo.add(promo)
        await self.session.commit()
        await self.session.refresh(promo)
        return promo

    async def update_promo(self, promo_id: int, payload: PromoUpdate) -> PromoCode:
        """Apply a partial update to a coupon.

        Args:
            promo_id: The promo to update.
            payload: The partial update (unset fields left unchanged).

        Returns:
            PromoCode: The updated promo.

        Raises:
            PromoNotFoundError: If the promo does not exist.
            PromoConflictError: If a new ``code`` collides with another coupon.
            PromoValidationError: If the resulting definition is inconsistent.
        """
        promo = await self.get_promo(promo_id)
        changes = payload.model_dump(exclude_unset=True)

        new_code = changes.get("code")
        if new_code is not None and new_code != promo.code:
            clash = await self.repo.get_by_code(new_code)
            if clash is not None and clash.id != promo.id:
                raise PromoConflictError(f"Promo code already exists: {new_code}")

        for field, value in changes.items():
            setattr(promo, field, value)

        self._validate_definition(
            discount_type=promo.discount_type,
            discount_value=promo.discount_value,
            active_from=promo.active_from,
            active_to=promo.active_to,
        )
        await self.session.commit()
        await self.session.refresh(promo)
        return promo

    async def delete_promo(self, promo_id: int) -> None:
        """Delete a coupon (existing orders keep their text snapshot, §2.5).

        Args:
            promo_id: The promo to delete.

        Raises:
            PromoNotFoundError: If the promo does not exist.
        """
        promo = await self.get_promo(promo_id)
        await self.session.delete(promo)
        await self.session.commit()

    @staticmethod
    def _validate_definition(
        *,
        discount_type: str,
        discount_value: Decimal,
        active_from: datetime,
        active_to: datetime,
    ) -> None:
        """Guard the cross-field invariants of a coupon definition.

        Args:
            discount_type: ``percent`` or ``fixed``.
            discount_value: The discount magnitude.
            active_from: Window start.
            active_to: Window end.

        Raises:
            PromoValidationError: If the type is unknown, a percent exceeds 100,
                or the window is inverted.
        """
        if discount_type not in DISCOUNT_TYPES:
            raise PromoValidationError(f"Unknown discount_type: {discount_type}")
        if discount_type == _PERCENT and discount_value > _HUNDRED:
            raise PromoValidationError("Percent discount cannot exceed 100")
        if active_to < active_from:
            raise PromoValidationError("active_to must not precede active_from")
