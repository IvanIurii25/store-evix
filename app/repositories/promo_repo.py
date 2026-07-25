"""Async data-access layer for the promo-code domain (feature A1, §2.5).

Only SQL / query construction lives here — no discount rules (those belong to
:class:`~app.services.promo_service.PromoService`). All statements are async
SQLAlchemy 2.0 bound to one session.

The usage counter is *derived*, not stored: :meth:`count_orders_using` counts
``order`` rows whose ``promo_code`` snapshot equals the code, so the usage limit
is enforced without a stale counter column on ``promo_code`` (the code is a soft
text link on the order — see ``app/models/order.py``).
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.models.promo import PromoCode


class PromoRepository:
    """Read/write queries for promo codes, bound to one session."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the session used by every query method.

        Args:
            session: Active async session (request-scoped or test-scoped).
        """
        self.session = session

    async def get_by_code(self, code: str) -> PromoCode | None:
        """Return the promo row for an exact ``code``, or ``None``.

        Args:
            code: The coupon code as typed at checkout (matched case-sensitively
                against the unique ``promo_code.code`` column).

        Returns:
            PromoCode | None: The matching row, or ``None`` when unknown.
        """
        stmt = select(PromoCode).where(PromoCode.code == code)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, promo_id: int) -> PromoCode | None:
        """Return a promo by primary key, or ``None``.

        Args:
            promo_id: The promo primary key.

        Returns:
            PromoCode | None: The row, or ``None``.
        """
        stmt = select(PromoCode).where(PromoCode.id == promo_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_all(self) -> list[PromoCode]:
        """Return every promo code (newest id first) for the admin roster.

        Returns:
            list[PromoCode]: All promo rows.
        """
        stmt = select(PromoCode).order_by(PromoCode.id.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_orders_using(self, code: str) -> int:
        """Count orders that redeemed a code (derived usage counter, §2.5).

        Args:
            code: The coupon code snapshotted onto orders.

        Returns:
            int: Number of orders whose ``promo_code`` equals ``code``.
        """
        stmt = select(func.count()).where(Order.promo_code == code)
        return int((await self.session.scalar(stmt)) or 0)

    def add(self, promo: PromoCode) -> PromoCode:
        """Stage a new promo row (not yet flushed).

        Args:
            promo: The promo instance to persist.

        Returns:
            PromoCode: The same instance, added to the session.
        """
        self.session.add(promo)
        return promo
