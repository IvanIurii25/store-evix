"""Reference-data caching in the delivery service (Nova Post phase P1).

The cache exists so a typeahead cannot turn our public endpoint into one
third-party call per keystroke. It must therefore hit — and, just as important,
it must never become a new way for the shop to break: a Redis outage degrades to
uncached lookups, not to an error page.
"""

from decimal import Decimal

import pytest

from app.core.config import settings
from app.services.delivery.novapost_service import NovaPostService

pytestmark = pytest.mark.asyncio


class BrokenRedis:
    """A Redis whose every operation fails."""

    async def get(self, key: str) -> str:
        """Fail like an unreachable Redis would."""
        raise RuntimeError("redis down")

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        """Fail like an unreachable Redis would."""
        raise RuntimeError("redis down")


class GarbageRedis(dict):
    """A Redis holding a value that is not valid JSON."""

    async def get(self, key: str) -> str:
        """Return something the cache cannot decode."""
        return "{not json"

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        """Accept writes silently."""
        return None


@pytest.fixture(autouse=True)
def _stub_carrier(monkeypatch):
    """Run these against the stub carrier."""
    monkeypatch.setattr(settings, "novapost_mode", "stub")


async def test_broken_cache_still_serves_lookups() -> None:
    """A Redis outage costs the cache, not the feature."""
    service = NovaPostService(BrokenRedis())

    rows = await service.settlements("chi")

    assert [r.name for r in rows] == ["Chișinău"]


async def test_broken_cache_still_serves_divisions() -> None:
    """Same for the pickup-point lookup."""
    service = NovaPostService(BrokenRedis())

    rows = await service.divisions("s-1", "branch", "")

    assert len(rows) == 2


async def test_unreadable_cache_entry_is_ignored() -> None:
    """A corrupt cache entry falls through to the carrier instead of raising."""
    service = NovaPostService(GarbageRedis())

    rows = await service.settlements("chi")

    assert [r.name for r in rows] == ["Chișinău"]


async def test_parcel_uses_the_greater_of_actual_and_volumetric() -> None:
    """The parcel declares real weight AND volume, so the carrier can bill either.

    ecom-obr sets ``volumetricWeight = actualWeight``, which under-declares a
    bulky-but-light order — the difference comes back as an invoice.
    """
    service = NovaPostService(BrokenRedis())

    parcel = service.build_parcels([200, 300])[0]

    assert parcel["actualWeight"] == 500
    # 250×350×200 mm = 17 500 cm³; at the default divisor 5000 → 3.5 kg.
    assert parcel["volumetricWeight"] == 3500
    assert parcel["width"] and parcel["length"] and parcel["height"]


async def test_parcel_falls_back_to_the_default_weight() -> None:
    """An empty weight list is priced as the configured default, not as zero."""
    service = NovaPostService(BrokenRedis())

    assert service.build_parcels([])[0]["actualWeight"] > 0


async def test_shipment_payer_defaults_to_sender_without_a_contract(monkeypatch) -> None:
    """No contract number → we pay on handover; no invented fallback contract.

    The reference implementation falls back to a literal contract belonging to
    another merchant, which would bill a stranger.
    """
    monkeypatch.setattr(settings, "novapost_contract_number", "")
    service = NovaPostService(BrokenRedis())

    payload = service.build_shipment(
        order_number="20260731-000001",
        recipient_name="Ion",
        phone="+37360000000",
        email="a@b.c",
        delivery_type="branch",
        division_id="d-1",
        settlement_id=None,
        address_parts=None,
        weights_g=[1200],
        insurance=Decimal("499.00"),
    )

    assert payload["payerType"] == "Sender"
    assert "payerContractNumber" not in payload
    assert payload["recipient"]["divisionId"] == "d-1"
    assert payload["parcels"][0]["insuranceCost"] == "499.00"
    assert payload["clientOrder"] == "20260731-000001"


async def test_shipment_bills_the_contract_when_configured(monkeypatch) -> None:
    """With a contract number the carrier bills that contract."""
    monkeypatch.setattr(settings, "novapost_contract_number", "CNPMD-1")
    service = NovaPostService(BrokenRedis())

    payload = service.build_shipment(
        order_number="X",
        recipient_name="Ion",
        phone="p",
        email="e",
        delivery_type="courier",
        division_id=None,
        settlement_id="s-1",
        address_parts={"street": "str. A", "building": "1"},
        weights_g=[500],
        insurance=Decimal("10"),
    )

    assert payload["payerType"] == "ThirdPerson"
    assert payload["payerContractNumber"] == "CNPMD-1"
    # A courier shipment addresses a settlement, not a pickup point.
    assert payload["recipient"]["settlementId"] == "s-1"
    assert "divisionId" not in payload["recipient"]


async def test_tracking_maps_numbers_to_status() -> None:
    """Tracking is reduced to {waybill: (code, text)} for the sweep to store."""
    service = NovaPostService(BrokenRedis())

    statuses = await service.tracking(["STUB00000001"])

    assert statuses["STUB00000001"] == ("10", "Accepted")
