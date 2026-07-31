"""Nova Post HTTP client transport behaviour (Nova Post phase P1).

No network: the client's ``httpx.AsyncClient`` is routed through a
``MockTransport``, and Redis is a tiny in-memory stand-in. What matters here is
the transport contract — token caching, the single retry when a cached token
died early, and turning every carrier failure into one domain error with a
message a human can act on.

The payload-shape leniency (``data`` vs ``items``) is pinned deliberately: the
endpoint contract is reconstructed from ecom-obr rather than official docs, and
this is the seam where that guess will break first.
"""

import json

import httpx
import pytest

from app.core.config import settings
from app.services.delivery import novapost_client as client_module
from app.services.delivery.novapost_client import NovaPostClient, NovaPostError

pytestmark = pytest.mark.asyncio


class FakeRedis:
    """Minimal async get/set store standing in for Redis."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.sets = 0

    async def get(self, key: str) -> str | None:
        """Return a stored value or ``None``."""
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        """Store a value, counting writes so tests can assert caching."""
        self.store[key] = value
        self.sets += 1


def _install(monkeypatch, handler) -> None:
    """Route the client's internal ``httpx.AsyncClient`` through ``handler``."""
    real = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(client_module.httpx, "AsyncClient", _factory)


def _client(redis: FakeRedis) -> NovaPostClient:
    """Build a client pointed at a fake base URL."""
    instance = NovaPostClient(redis, lang="ru")
    instance.base_url = "https://np.test/v.1.0"
    return instance


async def test_token_is_minted_once_and_cached(monkeypatch) -> None:
    """Two lookups share one authorization round-trip via the Redis cache."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/clients/authorization"):
            return httpx.Response(200, json={"jwt": "tok-1"})
        return httpx.Response(200, json={"data": []})

    _install(monkeypatch, handler)
    redis = FakeRedis()
    api = _client(redis)

    await api.settlements("a")
    await api.settlements("b")

    assert calls.count("/v.1.0/clients/authorization") == 1
    assert redis.store["np:jwt"] == "tok-1"


async def test_expired_token_is_reminted_once(monkeypatch) -> None:
    """A 401 on a cached token triggers exactly one re-mint and a retry."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/clients/authorization"):
            return httpx.Response(200, json={"jwt": "fresh"})
        seen.append(request.headers.get("Authorization", ""))
        if seen[-1] == "stale":
            return httpx.Response(401, json={"errors": [{"message": "expired"}]})
        return httpx.Response(200, json={"data": [{"id": "s-1", "name": "Chișinău"}]})

    _install(monkeypatch, handler)
    redis = FakeRedis()
    redis.store["np:jwt"] = "stale"
    api = _client(redis)

    rows = await api.settlements("chi")

    assert [r["name"] for r in rows] == ["Chișinău"]
    assert seen == ["stale", "fresh"]


async def test_persistent_401_becomes_a_domain_error(monkeypatch) -> None:
    """A token the carrier keeps rejecting surfaces as NovaPostError, not a loop."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/clients/authorization"):
            return httpx.Response(200, json={"jwt": "whatever"})
        return httpx.Response(401, json={"errors": [{"message": "nope"}]})

    _install(monkeypatch, handler)
    api = _client(FakeRedis())

    with pytest.raises(NovaPostError) as excinfo:
        await api.settlements("chi")

    assert excinfo.value.status_code == 401


async def test_missing_token_is_reported(monkeypatch) -> None:
    """An authorization response without a token fails fast and clearly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    _install(monkeypatch, handler)
    api = _client(FakeRedis())

    with pytest.raises(NovaPostError, match="no token"):
        await api.settlements("chi")


async def test_carrier_error_message_is_surfaced(monkeypatch) -> None:
    """The carrier's own description reaches the caller, not a generic 502 text."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/clients/authorization"):
            return httpx.Response(200, json={"jwt": "tok"})
        return httpx.Response(
            400, json={"errors": [{"description": "settlement not found"}]}
        )

    _install(monkeypatch, handler)
    api = _client(FakeRedis())

    with pytest.raises(NovaPostError, match="settlement not found"):
        await api.divisions("s-9", "branch")


async def test_timeout_becomes_a_domain_error(monkeypatch) -> None:
    """A stalled carrier raises NovaPostError instead of leaking httpx."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/clients/authorization"):
            return httpx.Response(200, json={"jwt": "tok"})
        raise httpx.TimeoutException("too slow")

    _install(monkeypatch, handler)
    api = _client(FakeRedis())

    with pytest.raises(NovaPostError, match="timed out"):
        await api.settlements("chi")


@pytest.mark.parametrize("key", ["data", "items"])
async def test_rows_accepted_under_either_key(monkeypatch, key: str) -> None:
    """Row lists are read whether the carrier wraps them in ``data`` or ``items``.

    The reference implementation uses both; until the official contract is
    confirmed, accepting either beats silently returning an empty list.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/clients/authorization"):
            return httpx.Response(200, json={"jwt": "tok"})
        return httpx.Response(200, json={key: [{"id": "s-1", "name": "Bălți"}]})

    _install(monkeypatch, handler)
    api = _client(FakeRedis())

    rows = await api.settlements("b")

    assert rows[0]["name"] == "Bălți"


async def test_division_by_id_swallows_failures(monkeypatch) -> None:
    """A failed snapshot lookup returns ``None`` — it must not block an order."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/clients/authorization"):
            return httpx.Response(200, json={"jwt": "tok"})
        return httpx.Response(500, text="boom")

    _install(monkeypatch, handler)
    api = _client(FakeRedis())

    assert await api.division_by_id("d-1") is None


async def test_accept_language_is_forwarded(monkeypatch) -> None:
    """The request language reaches the carrier — it localizes the names."""
    langs: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/clients/authorization"):
            return httpx.Response(200, json={"jwt": "tok"})
        langs.append(request.headers.get("Accept-language", ""))
        return httpx.Response(200, json={"data": []})

    _install(monkeypatch, handler)
    api = _client(FakeRedis())

    await api.settlements("x")

    assert langs == ["ru"]


async def test_calculate_sends_sender_and_parcels(monkeypatch) -> None:
    """The cost call carries our sender branch and the parcel set."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/clients/authorization"):
            return httpx.Response(200, json={"jwt": "tok"})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"services": [{"cost": "75.00"}]})

    _install(monkeypatch, handler)
    monkeypatch.setattr(settings, "novapost_sender_division_id", "sender-1")
    monkeypatch.setattr(settings, "novapost_contract_number", "CNP-1")
    api = _client(FakeRedis())

    payload = await api.calculate({"divisionId": "d-1"}, [{"actualWeight": 900}])

    assert payload["services"][0]["cost"] == "75.00"
    body = bodies[0]
    assert body["sender"]["divisionId"] == "sender-1"
    assert body["payerContractNumber"] == "CNP-1"
    assert body["parcels"][0]["actualWeight"] == 900
    assert body["recipient"] == {"countryCode": "MD", "divisionId": "d-1"}


async def test_create_shipment_returns_the_waybill(monkeypatch) -> None:
    """A created shipment hands back the carrier's payload verbatim."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/clients/authorization"):
            return httpx.Response(200, json={"jwt": "tok"})
        return httpx.Response(200, json={"id": "1", "number": "AWB-1"})

    _install(monkeypatch, handler)
    api = _client(FakeRedis())

    assert (await api.create_shipment({"parcels": []}))["number"] == "AWB-1"


async def test_tracking_passes_every_number(monkeypatch) -> None:
    """All requested waybills go out in one query."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/clients/authorization"):
            return httpx.Response(200, json={"jwt": "tok"})
        seen.extend(request.url.params.get_list("numbers[]"))
        return httpx.Response(200, json={"items": []})

    _install(monkeypatch, handler)
    api = _client(FakeRedis())

    await api.tracking(["A", "B"])

    assert seen == ["A", "B"]


async def test_delete_shipment_tolerates_an_empty_body(monkeypatch) -> None:
    """A 204 with no body is a success, not a JSON error."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/clients/authorization"):
            return httpx.Response(200, json={"jwt": "tok"})
        return httpx.Response(204)

    _install(monkeypatch, handler)
    api = _client(FakeRedis())

    assert await api.delete_shipment("AWB-1") is None


async def test_connection_failure_becomes_a_domain_error(monkeypatch) -> None:
    """A refused connection is reported as NovaPostError, not httpx."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/clients/authorization"):
            return httpx.Response(200, json={"jwt": "tok"})
        raise httpx.ConnectError("refused")

    _install(monkeypatch, handler)
    api = _client(FakeRedis())

    with pytest.raises(NovaPostError, match="request failed"):
        await api.settlements("x")


async def test_authorization_failure_becomes_a_domain_error(monkeypatch) -> None:
    """A carrier that will not issue a token fails with a clear message."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="maintenance")

    _install(monkeypatch, handler)

    with pytest.raises(NovaPostError, match="authorization failed"):
        await _client(FakeRedis()).settlements("x")


async def test_non_json_body_is_reported(monkeypatch) -> None:
    """An HTML error page surfaces as its text, not as a decode crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/clients/authorization"):
            return httpx.Response(200, json={"jwt": "tok"})
        return httpx.Response(500, text="<html>gateway</html>")

    _install(monkeypatch, handler)

    with pytest.raises(NovaPostError, match="gateway"):
        await _client(FakeRedis()).settlements("x")
