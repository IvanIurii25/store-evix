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
