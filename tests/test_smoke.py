"""B0 smoke tests: app import, openapi, unified error envelope.

The DB-backed ``/api/v1/health`` route is exercised separately (needs Postgres);
here we cover what runs without a live DB.
"""

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app


def test_openapi_available() -> None:
    """OpenAPI schema is served and structurally valid."""
    with TestClient(app) as client:
        response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["openapi"].startswith("3.")
    assert "/api/v1/health" in schema["paths"]


def test_http_exception_envelope() -> None:
    """An HTTPException is wrapped in the unified error envelope."""
    app.add_api_route("/_boom_http", _raise_http, methods=["GET"])
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/_boom_http")
    assert response.status_code == 418
    body = response.json()
    assert body["error"]["code"] == "http_error"
    assert body["error"]["message"] == "teapot"


def test_unhandled_exception_envelope() -> None:
    """An uncaught exception becomes a generic 500 in the unified envelope."""
    app.add_api_route("/_boom_500", _raise_generic, methods=["GET"])
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/_boom_500")
    assert response.status_code == 500
    body = response.json()
    assert body == {
        "error": {"code": "internal_error", "message": "Internal server error"}
    }


async def _raise_http() -> None:
    """Route that raises an HTTPException for envelope testing."""
    raise HTTPException(status_code=418, detail="teapot")


async def _raise_generic() -> None:
    """Route that raises an uncaught exception for 500-envelope testing."""
    raise RuntimeError("kaboom")
