"""Async HTTP client for the Nova Post delivery API (v.1.0).

A thin transport adapter over the endpoints the store needs: settlement /
division lookup, shipment cost calculation, waybill create / cancel and
tracking. Every failure surfaces as one domain error, :class:`NovaPostError`, so
the service layer never sees an ``httpx`` exception.

Two deliberate departures from the reference implementation in ``ecom-obr``
(``service_credentials/novapost_integration_v2.py``):

* **The JWT is cached in Redis, not per instance.** There the client is built
  per call, so every lookup paid for a fresh ``/clients/authorization``
  round-trip. Here the token is shared across requests and workers.
* **Nothing is hardcoded.** The reference falls back to a literal contract
  number when configuration is missing; a missing contract is a configuration
  error, not something to paper over with someone else's account.

⚠️ The endpoint shapes below are reconstructed from that reference
implementation, not from Nova Post's official documentation (see the plan,
§10.3). They must be checked against the real API — and against a live sandbox —
before the first shipment is created.
"""

import logging
from typing import Any

import httpx
from redis.asyncio import Redis

from app.core.config import settings

logger = logging.getLogger(__name__)

# One round-trip cap (seconds): a stalled carrier must not hang a checkout.
_HTTP_TIMEOUT: float = 10.0

# Redis key holding the shared bearer token, and how long we trust it. The API
# does not state an expiry, so this is a conservative guess — a stale token
# simply causes one 401 and a re-mint, which the client handles.
_TOKEN_KEY: str = "np:jwt"
_TOKEN_TTL: int = 3_000  # 50 minutes

# Category name → the carrier's division categories.
_CATEGORY_MAP: dict[str, list[str]] = {
    "postomat": ["Postomat"],
    "branch": ["CargoBranch", "PostBranch"],
}


class NovaPostError(Exception):
    """Any transport / protocol failure talking to Nova Post (domain error).

    ``status_code`` is the HTTP status when the failure came from a non-2xx
    response, else ``None``.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class NovaPostClient:
    """Async adapter over the Nova Post REST API with a Redis-cached token.

    Args:
        redis: Client used to share the bearer token across requests/workers.
        lang: Value for ``Accept-language``; the carrier localizes settlement and
            branch names by it, which is what makes ru/ro snapshots possible.
    """

    def __init__(self, redis: Redis, *, lang: str = "ro") -> None:
        self.redis = redis
        self.lang = lang
        self.base_url = settings.novapost_base_url

    # ------------------------------------------------------------------ #
    # Reference data
    # ------------------------------------------------------------------ #
    async def settlements(self, query: str, *, limit: int = 15) -> list[dict]:
        """Return settlements (cities) matching a free-text query.

        Args:
            query: What the customer typed.
            limit: Maximum rows to ask the carrier for.

        Returns:
            list[dict]: Raw carrier rows (``id`` + localized ``name``).
        """
        payload = await self._get(
            "/settlements",
            params={
                "countryCodes[]": "MD",
                "textSearch": query,
                "limit": limit,
                "page": 1,
            },
        )
        return _rows(payload)

    async def divisions(
        self,
        settlement_id: str,
        category: str,
        *,
        query: str = "",
        limit: int = 25,
    ) -> list[dict]:
        """Return pickup points of one category inside a settlement.

        Args:
            settlement_id: Carrier settlement id from :meth:`settlements`.
            category: ``branch`` or ``postomat`` (``courier`` has no points).
            query: Optional free-text narrowing (street, number).
            limit: Maximum rows to ask the carrier for.

        Returns:
            list[dict]: Raw carrier rows.
        """
        return _rows(
            await self._get(
                "/divisions",
                params={
                    "countryCodes[]": "MD",
                    "settlementIds[]": settlement_id,
                    "divisionCategories[]": _CATEGORY_MAP.get(
                        category, _CATEGORY_MAP["branch"]
                    ),
                    "textSearch": query,
                    "limit": limit,
                    "page": 1,
                },
            )
        )

    async def division_by_id(self, division_id: str) -> dict | None:
        """Return one pickup point by id, or ``None`` if it cannot be resolved.

        Used when snapshotting an order: the human-readable city/branch address
        is stored on the order so it survives without calling the carrier again.
        """
        try:
            rows = _rows(
                await self._get("/divisions", params={"ids[]": division_id, "limit": 1})
            )
        except NovaPostError:
            # A failed lookup must not block order creation — the caller keeps
            # the ids and simply has no pretty address to show.
            logger.warning("novapost: division %s could not be resolved", division_id)
            return None
        return rows[0] if rows else None

    # ------------------------------------------------------------------ #
    # Money + shipments (used from phase P2 onwards)
    # ------------------------------------------------------------------ #
    async def calculate(self, recipient: dict, parcels: list[dict]) -> dict:
        """Return the carrier's price breakdown for a recipient + parcel set."""
        return await self._post(
            "/shipments/calculations",
            json={
                "payerType": "ThirdPerson",
                "payerContractNumber": settings.novapost_contract_number,
                "parcels": parcels,
                "sender": {
                    "countryCode": "MD",
                    "divisionId": settings.novapost_sender_division_id,
                },
                "recipient": {"countryCode": "MD", **recipient},
            },
        )

    async def create_shipment(self, payload: dict) -> dict:
        """Create a waybill and return the carrier's response."""
        return await self._post("/shipments", json=payload)

    async def tracking(self, numbers: list[str]) -> dict:
        """Return tracking statuses for the given waybill numbers."""
        return await self._get("/shipments/tracking", params={"numbers[]": numbers})

    async def delete_shipment(self, shipment_id: str) -> None:
        """Cancel a waybill."""
        await self._request("DELETE", f"/shipments/{shipment_id}")

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #
    async def _token(self, *, force: bool = False) -> str:
        """Return a bearer token, minting a new one when absent or forced.

        Args:
            force: Skip the cache (used once after a 401, in case the cached
                token died early).

        Returns:
            str: The bearer token.

        Raises:
            NovaPostError: If the carrier does not hand out a token.
        """
        if not force:
            cached = await self.redis.get(_TOKEN_KEY)
            if cached:
                return cached
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            try:
                response = await client.get(
                    f"{self.base_url}/clients/authorization",
                    params={"apiKey": settings.novapost_api_token},
                )
                response.raise_for_status()
                token = (response.json() or {}).get("jwt")
            except httpx.HTTPError as exc:
                raise NovaPostError(f"Nova Post authorization failed: {exc}") from exc
            except ValueError as exc:
                raise NovaPostError("Nova Post returned invalid JSON") from exc
        if not token:
            raise NovaPostError("Nova Post returned no token")
        await self.redis.set(_TOKEN_KEY, token, ex=_TOKEN_TTL)
        return token

    async def _get(self, path: str, *, params: dict | None = None) -> Any:
        """Perform an authenticated GET."""
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, *, json: dict) -> Any:
        """Perform an authenticated POST."""
        return await self._request("POST", path, json=json)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> Any:
        """Send one authenticated request, retrying once on a 401.

        A 401 means the cached token died before its TTL; the retry re-mints it.
        Any other failure becomes a :class:`NovaPostError`.

        Args:
            method: HTTP verb.
            path: Path under the API base URL.
            params: Query parameters.
            json: JSON body.

        Returns:
            Any: The decoded JSON body (``None`` for an empty response).

        Raises:
            NovaPostError: On timeout, connection error, non-2xx or bad JSON.
        """
        for attempt in (1, 2):
            token = await self._token(force=attempt == 2)
            try:
                async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                    response = await client.request(
                        method,
                        f"{self.base_url}{path}",
                        params=params,
                        json=json,
                        headers={
                            "Authorization": token,
                            "Accept-language": self.lang,
                        },
                    )
                if response.status_code == 401 and attempt == 1:
                    continue
                response.raise_for_status()
                if not response.content:
                    return None
                return response.json()
            except httpx.TimeoutException as exc:
                raise NovaPostError(f"Nova Post timed out on {path}") from exc
            except httpx.HTTPStatusError as exc:
                raise NovaPostError(
                    _describe(exc.response), status_code=exc.response.status_code
                ) from exc
            except httpx.HTTPError as exc:
                raise NovaPostError(f"Nova Post request failed: {exc}") from exc
            except ValueError as exc:
                raise NovaPostError("Nova Post returned invalid JSON") from exc
        raise NovaPostError("Nova Post rejected the token twice", status_code=401)


def _rows(payload: Any) -> list[dict]:
    """Return the row list from a carrier payload, whatever key it arrived under.

    The reference implementation reads ``data`` in one place and ``items`` in
    another; until the contract is confirmed against the official docs we accept
    both rather than silently returning nothing.
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    rows = payload.get("data") or payload.get("items") or []
    return rows if isinstance(rows, list) else []


def _describe(response: httpx.Response) -> str:
    """Return the most specific error message the carrier's body offers."""
    try:
        body = response.json()
    except ValueError:
        return response.text or f"Nova Post HTTP {response.status_code}"
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return str(first.get("description") or first.get("message") or first)
        return str(first)
    if isinstance(body, dict):
        for key in ("message", "error", "detail"):
            if body.get(key):
                return str(body[key])
    return response.text or f"Nova Post HTTP {response.status_code}"
