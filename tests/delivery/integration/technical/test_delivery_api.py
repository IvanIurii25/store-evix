"""Public delivery endpoints (Nova Post phase P1).

Runs against the stub carrier, which is exactly how the feature is meant to be
developed before credentials exist. Pins the three properties the storefront
depends on: own methods survive the carrier being off, the lookups are shut
when it is off, and results are cached so a search box cannot turn our public
endpoint into a free proxy for a third-party API.
"""

import pytest
from httpx import AsyncClient

from app.core.config import settings

pytestmark = pytest.mark.asyncio

_METHODS = "/api/v1/delivery/methods"
_SETTLEMENTS = "/api/v1/delivery/novapost/settlements"
_DIVISIONS = "/api/v1/delivery/novapost/divisions"


@pytest.fixture
def carrier_on(monkeypatch):
    """Enable the stub carrier for one test."""
    monkeypatch.setattr(settings, "novapost_mode", "stub")


@pytest.fixture
def carrier_off(monkeypatch):
    """Disable the carrier entirely for one test."""
    monkeypatch.setattr(settings, "novapost_mode", "")


async def test_methods_without_carrier_lists_own_options(
    client: AsyncClient, carrier_off
) -> None:
    """With the carrier off the shop still offers pickup and its own courier."""
    resp = await client.get(_METHODS)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["novapost_enabled"] is False
    assert {(m["service"], m["type"]) for m in body["methods"]} == {
        ("own", "pickup"),
        ("own", "courier"),
    }


async def test_methods_with_carrier_adds_its_options(
    client: AsyncClient, carrier_on
) -> None:
    """The enabled carrier contributes its pickup categories."""
    resp = await client.get(_METHODS)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["novapost_enabled"] is True
    pairs = {(m["service"], m["type"]) for m in body["methods"]}
    assert ("novapost", "branch") in pairs
    assert ("novapost", "postomat") in pairs
    assert ("own", "pickup") in pairs


async def test_courier_method_carries_the_address_contract(
    client: AsyncClient, carrier_on
) -> None:
    """The courier option ships its field spec so the form isn't guessed."""
    resp = await client.get(_METHODS)

    courier = next(
        m
        for m in resp.json()["methods"]
        if m["service"] == "novapost" and m["type"] == "courier"
    )
    required = {f["name"] for f in courier["address_fields"] if f["required"]}
    assert required == {"city", "street", "building", "postCode"}


async def test_only_configured_categories_are_offered(
    client: AsyncClient, monkeypatch, carrier_on
) -> None:
    """Turning a category off in config removes it from the offer."""
    monkeypatch.setattr(settings, "novapost_division_categories", "branch")

    resp = await client.get(_METHODS)

    types = {m["type"] for m in resp.json()["methods"] if m["service"] == "novapost"}
    assert types == {"branch"}


async def test_lookups_are_closed_when_carrier_is_off(
    client: AsyncClient, carrier_off
) -> None:
    """A disabled integration exposes no lookup surface at all."""
    settlements = await client.post(_SETTLEMENTS, json={"query": "chi"})
    divisions = await client.post(
        _DIVISIONS, json={"settlement_id": "s-1", "category": "branch"}
    )

    assert settlements.status_code == 404, settlements.text
    assert settlements.json()["error"]["code"] == "novapost_disabled"
    assert divisions.status_code == 404, divisions.text


async def test_settlement_lookup_returns_cities(
    client: AsyncClient, carrier_on
) -> None:
    """The city picker gets id + localized name."""
    resp = await client.post(_SETTLEMENTS, json={"query": "chi"})

    assert resp.status_code == 200, resp.text
    rows = resp.json()["data"]
    assert [r["name"] for r in rows] == ["Chișinău"]
    assert rows[0]["id"]


async def test_division_lookup_is_scoped_by_category(
    client: AsyncClient, carrier_on
) -> None:
    """Branches and postomats are separate lists, with addresses attached."""
    branches = await client.post(
        _DIVISIONS, json={"settlement_id": "s-1", "category": "branch"}
    )
    postomats = await client.post(
        _DIVISIONS, json={"settlement_id": "s-1", "category": "postomat"}
    )

    assert branches.status_code == 200, branches.text
    assert len(branches.json()["data"]) == 2
    assert branches.json()["data"][0]["address"]
    assert len(postomats.json()["data"]) == 1


async def test_unknown_category_is_rejected(client: AsyncClient, carrier_on) -> None:
    """``courier`` has no pickup points, so it is not a valid lookup category."""
    resp = await client.post(
        _DIVISIONS, json={"settlement_id": "s-1", "category": "courier"}
    )

    assert resp.status_code == 422, resp.text


async def test_second_lookup_is_served_from_cache(
    client: AsyncClient, carrier_on, monkeypatch
) -> None:
    """A repeated query does not reach the carrier again.

    Guards the reason the cache exists: a typeahead would otherwise issue one
    third-party call per keystroke.
    """
    from app.services.delivery import novapost_stub

    calls: list[str] = []
    original = novapost_stub.NovaPostStub.settlements

    async def counting(self, query: str, *, limit: int = 15):
        calls.append(query)
        return await original(self, query, limit=limit)

    monkeypatch.setattr(novapost_stub.NovaPostStub, "settlements", counting)

    first = await client.post(_SETTLEMENTS, json={"query": "bal-cache-test"})
    second = await client.post(_SETTLEMENTS, json={"query": "bal-cache-test"})

    assert first.status_code == 200 and second.status_code == 200
    assert calls == ["bal-cache-test"]


async def test_languages_do_not_share_a_cache_entry(
    client: AsyncClient, carrier_on, monkeypatch
) -> None:
    """ru and ro are cached apart — the carrier localizes the names."""
    from app.services.delivery import novapost_stub

    calls: list[str] = []
    original = novapost_stub.NovaPostStub.settlements

    async def counting(self, query: str, *, limit: int = 15):
        calls.append(self.lang)
        return await original(self, query, limit=limit)

    monkeypatch.setattr(novapost_stub.NovaPostStub, "settlements", counting)

    await client.post(f"{_SETTLEMENTS}?lang=ru", json={"query": "lang-cache-test"})
    await client.post(f"{_SETTLEMENTS}?lang=ro", json={"query": "lang-cache-test"})

    assert calls == ["ru", "ro"]


async def test_carrier_failure_becomes_502(
    client: AsyncClient, carrier_on, monkeypatch
) -> None:
    """An unreachable carrier is a clean 502, not a 500 stack trace."""
    from app.services.delivery import novapost_stub
    from app.services.delivery.novapost_client import NovaPostError

    async def boom(self, query: str, *, limit: int = 15):
        raise NovaPostError("carrier down")

    monkeypatch.setattr(novapost_stub.NovaPostStub, "settlements", boom)

    resp = await client.post(_SETTLEMENTS, json={"query": "unique-failure-query"})

    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "delivery_lookup_unavailable"


async def test_lookups_are_rate_limited(client: AsyncClient, carrier_on) -> None:
    """The public proxy has a per-IP budget.

    The budget is read from settings rather than patched: the limiter is built
    when the router module is imported, so a later ``monkeypatch`` of the
    setting would silently do nothing and the test would pass for the wrong
    reason.
    """
    budget = int(settings.rate_limit_delivery.split("/")[0])

    statuses = [
        (await client.post(_SETTLEMENTS, json={"query": f"rl-{i}"})).status_code
        for i in range(budget + 1)
    ]

    assert statuses[-1] == 429
    assert statuses.count(200) == budget
