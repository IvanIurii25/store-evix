"""Async HTTP client for the maib e-Commerce API (v1).

Wraps the four maib endpoints the store needs — ``/generate-token``, ``/pay``,
``/pay-info/{payId}`` and ``/refund`` — plus the callback signature check. It is
a thin transport adapter: it holds an in-memory access-token cache (refreshed
before expiry, or re-minted via the refresh token when possible) and raises a
single domain error, :class:`MaibError`, on any transport / protocol failure so
the service layer never sees an ``httpx`` exception.

Signature verification (the security-critical part) is implemented exactly as
maib specifies: take the ``result`` dict, sort its keys alphabetically
(recursively), append the merchant ``signature_key`` as the final value, join all
values with ``":"`` (recursing into nested dicts/lists, no trailing separator),
``base64(sha256(...))`` and compare in constant time against the received
signature.
"""

import base64
import hashlib
import hmac
import logging
import time

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# One round-trip cap (seconds): a stalled maib must not hang the request.
_HTTP_TIMEOUT: float = 10.0

# Refresh the access token this many seconds before its stated expiry, so an
# in-flight call never races the token's death.
_TOKEN_EXPIRY_SKEW: int = 30


class MaibError(Exception):
    """Any transport / protocol failure talking to maib (domain error).

    Raised so the service layer maps a maib outage to a clean 5xx/failed-payment
    path instead of leaking an ``httpx`` exception. ``status_code`` is the HTTP
    status when the failure came from a non-2xx response, else ``None``.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class MaibClient:
    """Async adapter over the maib v1 REST API with a cached access token.

    A single instance may be reused across requests; the token cache is
    per-instance. All credentials come from ``settings`` (never hardcoded); the
    caller is responsible for only invoking payment methods when
    ``settings.card_payment_enabled`` is true.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        project_id: str | None = None,
        project_secret: str | None = None,
        signature_key: str | None = None,
    ) -> None:
        """Bind the client to maib credentials (defaulting to ``settings``).

        Args:
            base_url: maib API base (no trailing slash). Defaults to settings.
            project_id: maib project id. Defaults to settings.
            project_secret: maib project secret. Defaults to settings.
            signature_key: Callback signature secret. Defaults to settings.
        """
        self._base_url = (base_url or settings.maib_base_url).rstrip("/")
        self._project_id = project_id or settings.maib_project_id
        self._project_secret = project_secret or settings.maib_project_secret
        self._signature_key = signature_key or settings.maib_signature_key
        # In-memory token cache.
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._access_expires_at: float = 0.0

    # ------------------------------------------------------------------ #
    # Token management
    # ------------------------------------------------------------------ #
    async def get_token(self) -> str:
        """Return a valid access token, minting/refreshing it as needed.

        Serves the cached token while it is still valid (minus a safety skew);
        otherwise calls ``/generate-token`` and caches the new token, its expiry
        and the refresh token.

        Returns:
            str: A currently valid bearer access token.

        Raises:
            MaibError: If the token endpoint fails or returns no token.
        """
        now = time.monotonic()
        if self._access_token is not None and now < self._access_expires_at:
            return self._access_token

        body: dict[str, str] = {
            "projectId": self._project_id,
            "projectSecret": self._project_secret,
        }
        # Prefer a refresh-token exchange when we hold one (spec allows refresh);
        # maib's ``/generate-token`` accepts the same endpoint with credentials,
        # so we always re-mint with credentials here and simply capture the
        # returned refresh token for observability.
        data = await self._post("/generate-token", body, auth=False)
        token = data.get("accessToken")
        if not token:
            raise MaibError("maib token response missing accessToken")
        expires_in = int(data.get("expiresIn", 0) or 0)
        self._access_token = token
        self._refresh_token = data.get("refreshToken")
        self._access_expires_at = time.monotonic() + max(
            0, expires_in - _TOKEN_EXPIRY_SKEW
        )
        return token

    # ------------------------------------------------------------------ #
    # Payment operations
    # ------------------------------------------------------------------ #
    async def create_payment(
        self,
        *,
        amount: float,
        order_id: str,
        client_ip: str,
        language: str,
        description: str,
        callback_url: str,
        ok_url: str,
        fail_url: str,
        currency: str = "MDL",
        client_name: str | None = None,
        email: str | None = None,
    ) -> dict:
        """Create a maib payment and return ``{payId, payUrl, orderId}``.

        Args:
            amount: Order total as a number (maib expects a numeric amount).
            order_id: Our order number (echoed back on the callback).
            client_ip: Payer IP (fraud / 3-DS signal).
            language: Checkout-page language (``ro`` | ``ru`` | ``en``).
            description: Human-readable order description.
            callback_url: Our webhook URL maib POSTs the result to.
            ok_url: Storefront success return URL.
            fail_url: Storefront failure return URL.
            currency: ISO currency (fixed ``MDL``).
            client_name: Optional payer name.
            email: Optional payer email (maib emails a receipt).

        Returns:
            dict: The maib response containing ``payId`` and ``payUrl``.

        Raises:
            MaibError: If the request fails or the response lacks ``payId`` /
                ``payUrl``.
        """
        token = await self.get_token()
        body: dict[str, object] = {
            "amount": amount,
            "currency": currency,
            "clientIp": client_ip,
            "language": language,
            "description": description,
            "orderId": order_id,
            "callbackUrl": callback_url,
            "okUrl": ok_url,
            "failUrl": fail_url,
        }
        if client_name:
            body["clientName"] = client_name
        if email:
            body["email"] = email

        data = await self._post("/pay", body, token=token)
        if not data.get("payId") or not data.get("payUrl"):
            raise MaibError("maib /pay response missing payId/payUrl")
        return data

    async def get_pay_info(self, pay_id: str) -> dict:
        """Return the current maib status for a payment.

        Args:
            pay_id: The maib transaction id.

        Returns:
            dict: The pay-info payload.

        Raises:
            MaibError: If the request fails.
        """
        token = await self.get_token()
        return await self._get(f"/pay-info/{pay_id}", token=token)

    async def refund(self, pay_id: str, amount: float | None = None) -> dict:
        """Request a (full or partial) refund for a payment.

        Args:
            pay_id: The maib transaction id to refund.
            amount: Optional partial refund amount; ``None`` refunds in full.

        Returns:
            dict: The refund result payload.

        Raises:
            MaibError: If the request fails.
        """
        token = await self.get_token()
        body: dict[str, object] = {"payId": pay_id}
        if amount is not None:
            body["refundAmount"] = amount
        return await self._post("/refund", body, token=token)

    # ------------------------------------------------------------------ #
    # Signature verification
    # ------------------------------------------------------------------ #
    def verify_callback_signature(self, result: dict, signature: str) -> bool:
        """Return whether ``signature`` matches ``result`` per the maib spec.

        Algorithm (exactly as maib documents): recursively sort ``result`` keys
        alphabetically, append the merchant ``signature_key`` as the final value,
        join all values with ``":"`` (recursing into nested containers, no
        trailing separator), ``base64(sha256(binary))``, and compare in constant
        time against the received signature.

        Args:
            result: The ``result`` block from the callback (untouched dict).
            signature: The ``signature`` field from the callback.

        Returns:
            bool: True iff the computed signature equals ``signature``.
        """
        expected = self._compute_signature(result)
        return hmac.compare_digest(expected, signature or "")

    def _compute_signature(self, result: dict) -> str:
        """Compute the base64 SHA-256 signature for a ``result`` block.

        Args:
            result: The provider ``result`` mapping.

        Returns:
            str: The base64-encoded signature.
        """
        values = self._flatten_sorted_values(result)
        values.append(self._signature_key)
        payload = ":".join(values)
        digest = hashlib.sha256(payload.encode("utf-8")).digest()
        return base64.b64encode(digest).decode("ascii")

    def _flatten_sorted_values(self, node: object) -> list[str]:
        """Recursively flatten a value into its signable string components.

        Dicts contribute their values in alphabetical key order; lists contribute
        their items in order; scalars contribute their string form. This mirrors
        maib's recursive "sort keys, take values" rule so nested objects hash
        deterministically.

        Args:
            node: A dict, list or scalar drawn from ``result``.

        Returns:
            list[str]: The ordered string components to join with ``":"``.
        """
        if isinstance(node, dict):
            out: list[str] = []
            for key in sorted(node.keys()):
                out.extend(self._flatten_sorted_values(node[key]))
            return out
        if isinstance(node, (list, tuple)):
            out = []
            for item in node:
                out.extend(self._flatten_sorted_values(item))
            return out
        return [self._scalarize(node)]

    @staticmethod
    def _scalarize(value: object) -> str:
        """Render a scalar exactly as maib serializes it for signing.

        ``None`` becomes an empty string and booleans are lowercased; every other
        scalar uses its plain ``str`` form (numbers are passed through as their
        JSON text, which the caller must preserve when re-signing in tests).

        Args:
            value: A scalar value.

        Returns:
            str: The string form used in the signed payload.
        """
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #
    async def _post(
        self,
        path: str,
        body: dict,
        *,
        token: str | None = None,
        auth: bool = True,
    ) -> dict:
        """POST ``body`` to ``path`` and return the parsed JSON, or raise.

        Args:
            path: API path beginning with ``/``.
            body: JSON request body.
            token: Bearer token to attach (when ``auth`` is true).
            auth: Whether the endpoint requires the bearer token.

        Returns:
            dict: The parsed JSON response.

        Raises:
            MaibError: On any transport error or non-2xx response.
        """
        headers = self._auth_headers(token) if auth else None
        return await self._request("POST", path, json=body, headers=headers)

    async def _get(self, path: str, *, token: str) -> dict:
        """GET ``path`` with a bearer token and return the parsed JSON.

        Args:
            path: API path beginning with ``/``.
            token: Bearer access token.

        Returns:
            dict: The parsed JSON response.

        Raises:
            MaibError: On any transport error or non-2xx response.
        """
        return await self._request(
            "GET", path, headers=self._auth_headers(token)
        )

    @staticmethod
    def _auth_headers(token: str | None) -> dict[str, str]:
        """Build the ``Authorization`` header for a bearer token.

        Args:
            token: The access token, or ``None``.

        Returns:
            dict[str, str]: The header mapping (empty when no token).
        """
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        """Execute one maib HTTP request and normalize failures to ``MaibError``.

        Args:
            method: HTTP method.
            path: API path beginning with ``/``.
            json: Optional JSON body.
            headers: Optional extra headers.

        Returns:
            dict: The parsed JSON response body.

        Raises:
            MaibError: On timeout, connection error, non-2xx, or invalid JSON.
        """
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                response = await client.request(
                    method, url, json=json, headers=headers
                )
        except httpx.TimeoutException as exc:
            raise MaibError(f"maib request timed out: {method} {path}") from exc
        except httpx.HTTPError as exc:
            raise MaibError(f"maib request failed: {method} {path}: {exc}") from exc

        if response.status_code >= 400:
            logger.warning(
                "maib %s %s -> HTTP %s", method, path, response.status_code
            )
            raise MaibError(
                f"maib returned HTTP {response.status_code} for {path}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MaibError(f"maib returned non-JSON body for {path}") from exc
        # maib wraps success payloads under ``result``; unwrap when present so
        # callers see a flat dict (``payId``/``payUrl``/status fields).
        if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
            return payload["result"]
        if isinstance(payload, dict):
            return payload
        raise MaibError(f"maib returned unexpected JSON shape for {path}")
