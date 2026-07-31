"""Reference-data caching in the delivery service (Nova Post phase P1).

The cache exists so a typeahead cannot turn our public endpoint into one
third-party call per keystroke. It must therefore hit — and, just as important,
it must never become a new way for the shop to break: a Redis outage degrades to
uncached lookups, not to an error page.
"""

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
