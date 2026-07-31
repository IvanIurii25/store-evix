"""Nova Post reference-data service: lookups, caching, and the method list.

Sits between the router and the carrier backend (real client or dev stub). Its
job in phase P1 is the read side: which delivery methods the storefront may
offer, and the city / pickup-point lookups behind the checkout pickers. Pricing
and shipments arrive in later phases.

Lookups are cached in Redis because they are typed into a search box: without a
cache every keystroke would become a carrier request, turning our public
endpoint into a free proxy for their API. Cache failures are swallowed — a
degraded cache must slow the shop down, not break it.
"""

import json
import logging
from decimal import Decimal

from redis.asyncio import Redis

from app.core.config import settings
from app.schemas.delivery import (
    ADDRESS_FIELDS,
    BRANCH,
    COURIER,
    POSTOMAT,
    DeliveryMethodOut,
    DeliveryMethodsOut,
    DivisionOut,
    SettlementOut,
)
from app.services.delivery import new_novapost_client

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "np:ref"


class NovaPostService:
    """Read-side use-cases for the carrier (methods + reference data).

    Args:
        redis: Used for the shared bearer token and the lookup cache.
        lang: Requested language; forwarded to the carrier so settlement and
            branch names come back localized, and part of the cache key so ru
            and ro never serve each other's names.
    """

    def __init__(self, redis: Redis, *, lang: str = "ro") -> None:
        self.redis = redis
        self.lang = lang
        self.client = new_novapost_client(redis, lang=lang)

    # ------------------------------------------------------------------ #
    # Methods
    # ------------------------------------------------------------------ #
    def methods(self) -> DeliveryMethodsOut:
        """Return the delivery options currently on offer.

        Own pickup and own courier are always present — they have no external
        dependency, so the shop keeps selling even when the carrier is off or
        unreachable. Carrier options appear only when Nova Post is configured
        and the category is enabled.

        Returns:
            DeliveryMethodsOut: Methods plus the carrier's on/off state.
        """
        free_from = settings.free_delivery_from
        methods = [
            DeliveryMethodOut(service="own", type="pickup", flat_cost=Decimal("0")),
            DeliveryMethodOut(
                service="own",
                type=COURIER,
                flat_cost=settings.courier_rate,
                free_from=free_from,
            ),
        ]
        if settings.novapost_enabled:
            np_free_from = settings.novapost_free_delivery_from or free_from
            for category in settings.novapost_categories:
                if category not in (BRANCH, POSTOMAT, COURIER):
                    continue
                methods.append(
                    DeliveryMethodOut(
                        service="novapost",
                        type=category,
                        free_from=np_free_from,
                        address_fields=ADDRESS_FIELDS if category == COURIER else [],
                    )
                )
        return DeliveryMethodsOut(
            methods=methods, novapost_enabled=settings.novapost_enabled
        )

    # ------------------------------------------------------------------ #
    # Reference data
    # ------------------------------------------------------------------ #
    async def settlements(self, query: str) -> list[SettlementOut]:
        """Return cities matching ``query`` (cached).

        Args:
            query: Free text from the city picker.

        Returns:
            list[SettlementOut]: Matching settlements, possibly empty.

        Raises:
            NovaPostError: If the carrier is unreachable and nothing is cached.
        """
        key = f"{_CACHE_PREFIX}:s:{self.lang}:{query.strip().lower()}"
        cached = await self._cache_get(key)
        if cached is not None:
            return [SettlementOut(**row) for row in cached]
        rows = [
            SettlementOut(id=str(row.get("id")), name=str(row.get("name") or ""))
            for row in await self.client.settlements(query)
            if row.get("id")
        ]
        await self._cache_set(
            key, [r.model_dump() for r in rows], settings.novapost_settlements_ttl
        )
        return rows

    async def divisions(
        self, settlement_id: str, category: str, query: str
    ) -> list[DivisionOut]:
        """Return pickup points of one category in a settlement (cached).

        Args:
            settlement_id: Carrier settlement id.
            category: ``branch`` or ``postomat``.
            query: Optional free text (street / number).

        Returns:
            list[DivisionOut]: Matching pickup points, possibly empty.

        Raises:
            NovaPostError: If the carrier is unreachable and nothing is cached.
        """
        key = (
            f"{_CACHE_PREFIX}:d:{self.lang}:{settlement_id}:{category}:"
            f"{query.strip().lower()}"
        )
        cached = await self._cache_get(key)
        if cached is not None:
            return [DivisionOut(**row) for row in cached]
        rows = [
            DivisionOut(
                id=str(row.get("id")),
                number=str(row.get("number") or ""),
                address=str(row.get("address") or ""),
                settlement_name=str((row.get("settlement") or {}).get("name") or ""),
            )
            for row in await self.client.divisions(settlement_id, category, query=query)
            if row.get("id")
        ]
        await self._cache_set(
            key, [r.model_dump() for r in rows], settings.novapost_divisions_ttl
        )
        return rows

    # ------------------------------------------------------------------ #
    # Cache
    # ------------------------------------------------------------------ #
    async def _cache_get(self, key: str) -> list[dict] | None:
        """Return a cached row list, or ``None`` on a miss or unusable entry."""
        try:
            raw = await self.redis.get(key)
        except Exception:  # noqa: BLE001 — a broken cache must not break lookups
            logger.warning("novapost: cache read failed for %s", key, exc_info=True)
            return None
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, list) else None

    async def _cache_set(self, key: str, rows: list[dict], ttl: int) -> None:
        """Store a row list under ``key``; failures are logged and ignored."""
        try:
            await self.redis.set(key, json.dumps(rows), ex=ttl)
        except Exception:  # noqa: BLE001 — see above
            logger.warning("novapost: cache write failed for %s", key, exc_info=True)
