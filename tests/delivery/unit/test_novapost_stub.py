"""The development stub carrier (Nova Post phase P1).

The stub is what unblocks every delivery phase before the merchant contract
exists, so its behaviour is worth pinning: it must filter like a real lookup,
price by weight and distance, and never hand out the same waybill twice. If it
drifts, the phases built on top of it are being tested against nothing.
"""

from decimal import Decimal

import pytest

from app.services.delivery.novapost_stub import NovaPostStub

pytestmark = pytest.mark.asyncio


def _cost(payload: dict) -> Decimal:
    """Extract the quoted cost from a stub calculation payload."""
    return Decimal(payload["services"][0]["cost"])


async def test_settlements_filter_by_substring() -> None:
    """The city lookup matches on a case-insensitive substring."""
    stub = NovaPostStub()

    rows = await stub.settlements("chi")

    assert [r["name"] for r in rows] == ["Chișinău"]


async def test_settlements_empty_query_returns_all() -> None:
    """An empty query lists every known city (the picker's initial state)."""
    stub = NovaPostStub()

    rows = await stub.settlements("")

    assert len(rows) == 3


async def test_divisions_are_scoped_to_settlement_and_category() -> None:
    """Branches and postomats of one city do not leak into each other."""
    stub = NovaPostStub()

    branches = await stub.divisions("s-1", "branch")
    postomats = await stub.divisions("s-1", "postomat")

    assert {r["id"] for r in branches} == {"d-1", "d-2"}
    assert {r["id"] for r in postomats} == {"d-3"}
    assert all(r["settlement"]["name"] == "Chișinău" for r in branches)


async def test_division_by_id_resolves_city_name() -> None:
    """A point resolves to its address and city — what the order snapshots."""
    stub = NovaPostStub()

    division = await stub.division_by_id("d-4")

    assert division is not None
    assert division["settlement"]["name"] == "Bălți"
    assert division["address"] == "str. Independenței 3"


async def test_division_by_id_unknown_is_none() -> None:
    """An unknown id resolves to ``None`` rather than raising."""
    stub = NovaPostStub()

    assert await stub.division_by_id("nope") is None


async def test_cost_grows_with_weight() -> None:
    """A heavier parcel to the same point costs more."""
    stub = NovaPostStub()

    light = await stub.calculate({"divisionId": "d-1"}, [{"actualWeight": 1000}])
    heavy = await stub.calculate({"divisionId": "d-1"}, [{"actualWeight": 5000}])

    assert _cost(heavy) > _cost(light)


async def test_cost_grows_with_distance() -> None:
    """The same parcel costs more to a farther city."""
    stub = NovaPostStub()

    near = await stub.calculate({"divisionId": "d-1"}, [{"actualWeight": 1000}])
    far = await stub.calculate({"divisionId": "d-5"}, [{"actualWeight": 1000}])

    assert _cost(far) > _cost(near)


async def test_courier_costs_more_than_a_pickup_point() -> None:
    """Door delivery carries a surcharge over handing the parcel to a branch."""
    stub = NovaPostStub()

    branch = await stub.calculate({"divisionId": "d-1"}, [{"actualWeight": 1000}])
    courier = await stub.calculate(
        {"settlementId": "s-1", "addressParts": {"street": "x", "building": "1"}},
        [{"actualWeight": 1000}],
    )

    assert _cost(courier) > _cost(branch)


async def test_missing_weight_falls_back_to_one_kilo() -> None:
    """A parcel with no weight is priced as 1 kg, not as free."""
    stub = NovaPostStub()

    quoted = await stub.calculate({"divisionId": "d-1"}, [{}])
    one_kilo = await stub.calculate({"divisionId": "d-1"}, [{"actualWeight": 1000}])

    assert _cost(quoted) == _cost(one_kilo)


async def test_waybill_numbers_are_unique() -> None:
    """Two shipments never share a number (the idempotency guard relies on it)."""
    stub = NovaPostStub()

    first = await stub.create_shipment({})
    second = await stub.create_shipment({})

    assert first["number"] != second["number"]
    assert first["number"].startswith("STUB")


async def test_tracking_answers_for_every_requested_number() -> None:
    """Tracking returns one row per requested waybill."""
    stub = NovaPostStub()

    payload = await stub.tracking(["STUB00000001", "STUB00000002"])

    assert [item["number"] for item in payload["items"]] == [
        "STUB00000001",
        "STUB00000002",
    ]
