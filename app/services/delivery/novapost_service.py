"""Nova Post service: delivery methods, reference lookups, pricing, snapshots.

Sits between the router / checkout and the carrier backend (real client or dev
stub). Covers which methods the storefront may offer, the city and pickup-point
lookups behind the checkout pickers, the shipment price, and the destination
snapshot stored on an order. Waybills and tracking arrive with the admin phase.

Lookups are cached in Redis because they are typed into a search box: without a
cache every keystroke would become a carrier request, turning our public
endpoint into a free proxy for their API. Cache failures are swallowed — a
degraded cache must slow the shop down, not break it.
"""

import json
import logging
from decimal import ROUND_HALF_UP, Decimal

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
from app.services.delivery.novapost_client import NovaPostError

logger = logging.getLogger(__name__)

_CACHE_PREFIX = "np:ref"
# Millimetres cubed per cm³, used to turn the configured box into volume.
_MM3_PER_CM3 = 1000


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

    # ------------------------------------------------------------------ #
    # Money
    # ------------------------------------------------------------------ #
    def build_parcels(self, weights_g: list[int]) -> list[dict]:
        """Turn per-item weights into the single parcel we hand the carrier.

        Carriers bill the greater of actual and volumetric weight. ecom-obr sets
        the two equal, which under-declares any bulky-but-light order and comes
        back as an invoice; here the volumetric figure is computed from the
        configured box so the quote matches what will be charged.

        Args:
            weights_g: Effective weight of every cart item, already multiplied
                by quantity.

        Returns:
            list[dict]: One parcel dict in the carrier's shape.
        """
        actual = sum(weights_g) or settings.parcel_default_item_weight_g
        volume_cm3 = (
            settings.parcel_width_mm
            * settings.parcel_length_mm
            * settings.parcel_height_mm
        ) / _MM3_PER_CM3
        divisor = settings.parcel_volumetric_divisor or 1
        volumetric = int(volume_cm3 / divisor * 1000)
        return [
            {
                "cargoCategory": "parcel",
                "rowNumber": 1,
                "width": settings.parcel_width_mm,
                "length": settings.parcel_length_mm,
                "height": settings.parcel_height_mm,
                "actualWeight": actual,
                "volumetricWeight": volumetric,
            }
        ]

    async def quote(
        self,
        *,
        delivery_type: str,
        weights_g: list[int],
        division_id: str | None = None,
        settlement_id: str | None = None,
        address_parts: dict | None = None,
    ) -> Decimal:
        """Ask the carrier what this shipment costs.

        Args:
            delivery_type: ``branch``, ``postomat`` or ``courier``.
            weights_g: Effective per-item weights (quantity already applied).
            division_id: Pickup point (branch / postomat).
            settlement_id: City id (courier).
            address_parts: Courier address in the carrier's field layout.

        Returns:
            Decimal: The quoted cost, rounded to cents.

        Raises:
            NovaPostError: If the carrier is unreachable or rejects the request,
                or answers without a usable price. The caller turns this into a
                502 and does NOT invent a fallback — a silently wrong delivery
                charge is worse than a retry.
        """
        recipient: dict = (
            {"settlementId": settlement_id, "addressParts": address_parts or {}}
            if delivery_type == COURIER
            else {"divisionId": division_id}
        )
        payload = await self.client.calculate(recipient, self.build_parcels(weights_g))
        services = (payload or {}).get("services") or []
        if not services:
            raise NovaPostError("Nova Post returned no price for this shipment")
        raw = services[0].get("cost")
        if raw is None:
            raise NovaPostError("Nova Post returned a price without a cost")
        return Decimal(str(raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    async def snapshot(
        self,
        *,
        delivery_type: str,
        division_id: str | None,
        settlement_id: str | None,
    ) -> dict:
        """Resolve the human-readable destination stored on the order.

        Two lookups when a pickup point is involved: the carrier localizes names
        by ``Accept-language``, and the back-office reads orders in Russian even
        when the customer shopped in Romanian. A failed lookup degrades to empty
        names rather than blocking the order — the ids are already captured.

        Args:
            delivery_type: ``branch``, ``postomat`` or ``courier``.
            division_id: Pickup point id, when applicable.
            settlement_id: City id.

        Returns:
            dict: Snapshot fields for ``order_delivery_np``.
        """
        snap = {
            "settlement_id": settlement_id,
            "settlement_name": "",
            "settlement_name_ru": "",
            "division_id": division_id,
            "division_number": "",
            "division_address": "",
        }
        if delivery_type == COURIER or not division_id:
            return snap
        division = await self.client.division_by_id(division_id)
        if division:
            snap["division_number"] = str(division.get("number") or "")
            snap["division_address"] = str(division.get("address") or "")
            settlement = division.get("settlement") or {}
            snap["settlement_name"] = str(settlement.get("name") or "")
            snap["settlement_id"] = str(settlement.get("id") or settlement_id or "")
        ru_client = new_novapost_client(self.redis, lang="ru")
        division_ru = await ru_client.division_by_id(division_id)
        if division_ru:
            settlement_ru = division_ru.get("settlement") or {}
            snap["settlement_name_ru"] = str(settlement_ru.get("name") or "")
        return snap

    # ------------------------------------------------------------------ #
    # Shipments (waybills)
    # ------------------------------------------------------------------ #
    def build_shipment(
        self,
        *,
        order_number: str,
        recipient_name: str,
        phone: str,
        email: str,
        delivery_type: str,
        division_id: str | None,
        settlement_id: str | None,
        address_parts: dict | None,
        weights_g: list[int],
        insurance: Decimal,
    ) -> dict:
        """Assemble the waybill payload for one order.

        Args:
            order_number: Printed on the parcel so the carrier's paperwork and
                ours refer to the same thing.
            recipient_name: Who collects it.
            phone: Recipient phone (the carrier notifies by SMS).
            email: Recipient email.
            delivery_type: ``branch``, ``postomat`` or ``courier``.
            division_id: Pickup point, for a point delivery.
            settlement_id: City, for a courier delivery.
            address_parts: Courier address in the carrier's field layout.
            weights_g: Per-item weights the quote was based on.
            insurance: Declared value (the goods total).

        Returns:
            dict: The request body for ``POST /shipments``.
        """
        recipient: dict = {
            "companyTin": "",
            "companyName": "",
            "name": recipient_name,
            "phone": phone,
            "email": email,
            "countryCode": "MD",
        }
        if delivery_type == COURIER:
            recipient["settlementId"] = settlement_id
            recipient["addressParts"] = address_parts or {}
        else:
            recipient["divisionId"] = division_id

        parcels = self.build_parcels(weights_g)
        parcels[0]["parcelDescription"] = f"Order {order_number}"
        parcels[0]["insuranceCost"] = str(insurance)

        payload: dict = {
            "status": "ReadyToShip",
            "clientOrder": order_number,
            "note": f"Order {order_number}",
            "parcels": parcels,
            "sender": {
                "companyTin": "",
                "companyName": settings.novapost_sender_company,
                "name": settings.novapost_sender_name,
                "phone": settings.novapost_sender_phone,
                "email": settings.novapost_sender_email,
                "countryCode": "MD",
                "divisionId": settings.novapost_sender_division_id,
            },
            "recipient": recipient,
        }
        # Who pays. With a contract number the carrier bills that contract;
        # without one the sender (us) pays on handover. The reference hardcodes
        # a fallback contract belonging to another merchant — we do not.
        if settings.novapost_contract_number:
            payload["payerType"] = "ThirdPerson"
            payload["payerContractNumber"] = settings.novapost_contract_number
        else:
            payload["payerType"] = "Sender"
        return payload

    async def create_shipment(self, payload: dict) -> dict:
        """Create a waybill at the carrier and return its response."""
        return await self.client.create_shipment(payload)

    async def cancel_shipment(self, shipment_id: str) -> None:
        """Cancel a waybill at the carrier."""
        await self.client.delete_shipment(shipment_id)

    async def tracking(self, numbers: list[str]) -> dict[str, tuple[str, str]]:
        """Return ``{waybill: (status_code, status_text)}`` for the given numbers.

        Args:
            numbers: Waybill numbers to look up.

        Returns:
            dict: Only the entries the carrier answered for.
        """
        payload = await self.client.tracking(numbers)
        out: dict[str, tuple[str, str]] = {}
        for item in (payload or {}).get("items") or []:
            number = str(item.get("number") or "")
            current = item.get("currentStatus") or {}
            if number:
                out[number] = (
                    str(current.get("statusCode") or ""),
                    str(current.get("status") or ""),
                )
        return out
