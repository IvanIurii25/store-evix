"""Deterministic fake Nova Post carrier for development and tests.

The merchant contract (and with it the API key, the sender branch and the
sandbox) arrives on someone else's schedule. Without a stand-in, every phase
that touches delivery would be blocked on it — so this serves a small, fixed
catalogue of Moldovan cities and pickup points, prices parcels by weight with a
simple tariff, and issues synthetic waybill numbers.

It mirrors :class:`~app.services.delivery.novapost_client.NovaPostClient` method
for method, so the service layer never knows which one it holds. Selected by
``NOVAPOST_MODE=stub``; the settings validator refuses that value outside
``local``/``test``, so a fake carrier can never quote a real customer.

Deliberately *not* a mock library: the data is stable and readable, so a
developer can click through the storefront and recognize what they see, and a
test can assert on concrete values.
"""

from decimal import Decimal
from typing import Any

# Cities the stub knows, with a distance band that drives the price.
_SETTLEMENTS: list[dict[str, Any]] = [
    {"id": "s-1", "name": "Chișinău", "band": 0},
    {"id": "s-2", "name": "Bălți", "band": 1},
    {"id": "s-3", "name": "Cahul", "band": 2},
]

# Pickup points per settlement.
_DIVISIONS: list[dict[str, Any]] = [
    {
        "id": "d-1",
        "settlementId": "s-1",
        "category": "branch",
        "number": "1",
        "address": "str. Ștefan cel Mare 12",
    },
    {
        "id": "d-2",
        "settlementId": "s-1",
        "category": "branch",
        "number": "2",
        "address": "bd. Dacia 45",
    },
    {
        "id": "d-3",
        "settlementId": "s-1",
        "category": "postomat",
        "number": "101",
        "address": "str. Alba Iulia 75 (Kaufland)",
    },
    {
        "id": "d-4",
        "settlementId": "s-2",
        "category": "branch",
        "number": "1",
        "address": "str. Independenței 3",
    },
    {
        "id": "d-5",
        "settlementId": "s-3",
        "category": "postomat",
        "number": "102",
        "address": "str. Republicii 20",
    },
]

# Tariff: a base per distance band plus a per-kilogram rate, both in MDL.
_BASE_BY_BAND: dict[int, Decimal] = {
    0: Decimal("45"),
    1: Decimal("60"),
    2: Decimal("75"),
}
_PER_KG: Decimal = Decimal("8")
# Courier-to-the-door costs more than handing a parcel over at a branch.
_COURIER_SURCHARGE: Decimal = Decimal("25")


class NovaPostStub:
    """Stand-in carrier with the same surface as :class:`NovaPostClient`.

    Args:
        redis: Accepted and ignored, so the factory can build either backend
            with one call signature.
        lang: Accepted and ignored — the stub's names are already bilingual
            (Moldovan place names are spelled the same in both locales here).
    """

    def __init__(self, redis: Any = None, *, lang: str = "ro") -> None:
        self.redis = redis
        self.lang = lang
        # Incrementing waybill counter, so two shipments never share a number.
        self._issued = 0

    async def settlements(self, query: str, *, limit: int = 15) -> list[dict]:
        """Return the known cities whose name contains ``query``."""
        term = (query or "").strip().lower()
        rows = [
            {"id": s["id"], "name": s["name"]}
            for s in _SETTLEMENTS
            if not term or term in s["name"].lower()
        ]
        return rows[:limit]

    async def divisions(
        self,
        settlement_id: str,
        category: str,
        *,
        query: str = "",
        limit: int = 25,
    ) -> list[dict]:
        """Return the pickup points of one category in a settlement."""
        term = (query or "").strip().lower()
        rows = [
            {
                "id": d["id"],
                "number": d["number"],
                "address": d["address"],
                "settlement": {
                    "id": d["settlementId"],
                    "name": _city(d["settlementId"]),
                },
            }
            for d in _DIVISIONS
            if d["settlementId"] == settlement_id
            and d["category"] == category
            and (not term or term in d["address"].lower() or term == d["number"])
        ]
        return rows[:limit]

    async def division_by_id(self, division_id: str) -> dict | None:
        """Return one pickup point by id, or ``None`` when unknown."""
        for d in _DIVISIONS:
            if d["id"] == division_id:
                return {
                    "id": d["id"],
                    "number": d["number"],
                    "address": d["address"],
                    "settlement": {
                        "id": d["settlementId"],
                        "name": _city(d["settlementId"]),
                    },
                }
        return None

    async def calculate(self, recipient: dict, parcels: list[dict]) -> dict:
        """Return a price shaped like the carrier's, derived from weight + band.

        Args:
            recipient: Either ``{"divisionId": …}`` (pickup point) or
                ``{"settlementId": …, "addressParts": {…}}`` (courier).
            parcels: Parcel dicts carrying ``actualWeight`` in grams.

        Returns:
            dict: ``{"services": [{"cost": "<amount>"}]}``.
        """
        grams = sum(int(p.get("actualWeight") or 0) for p in parcels) or 1000
        settlement_id = recipient.get("settlementId")
        if not settlement_id and recipient.get("divisionId"):
            division = await self.division_by_id(str(recipient["divisionId"]))
            settlement_id = (division or {}).get("settlement", {}).get("id")
        band = next(
            (s["band"] for s in _SETTLEMENTS if s["id"] == settlement_id),
            1,
        )
        cost = _BASE_BY_BAND.get(band, Decimal("60"))
        cost += (Decimal(grams) / Decimal(1000)) * _PER_KG
        if recipient.get("addressParts"):
            cost += _COURIER_SURCHARGE
        return {"services": [{"cost": str(cost.quantize(Decimal("0.01")))}]}

    async def create_shipment(self, payload: dict) -> dict:
        """Issue a synthetic waybill."""
        self._issued += 1
        number = f"STUB{self._issued:08d}"
        return {"id": number, "number": number, "status": "ReadyToShip"}

    async def tracking(self, numbers: list[str]) -> dict:
        """Report every requested waybill as accepted by the carrier."""
        return {
            "items": [
                {
                    "number": number,
                    "currentStatus": {"status": "Accepted", "statusCode": "10"},
                }
                for number in numbers
            ]
        }

    async def delete_shipment(self, shipment_id: str) -> None:
        """Accept a cancellation (nothing to undo in the stub)."""
        return None


def _city(settlement_id: str) -> str:
    """Return a settlement's display name, or an empty string when unknown."""
    for s in _SETTLEMENTS:
        if s["id"] == settlement_id:
            return str(s["name"])
    return ""
