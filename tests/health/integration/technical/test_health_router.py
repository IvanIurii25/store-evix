"""Router-level test for the ``/health`` endpoint (lines 22-23).

Self-contained: it builds a minimal local FastAPI app mounting *only* the health
router (mirroring how the domain conftests mount a single router) and overrides
``get_session`` with the shared transactional ``db_session`` so the endpoint's
``SELECT 1`` connectivity probe runs against the real test database.

The degraded/DB-down branch is intentionally not exercised — the endpoint has no
such branch: ``session.execute`` simply propagates on failure (handled by the
app's error layer), so simulating a failing session would only cover framework
machinery, not a distinct router line.
"""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.health import router as health_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers

pytestmark = pytest.mark.asyncio

_API_V1_PREFIX = "/api/v1"
# Expected healthy response.
_HTTP_OK: int = 200
_OK_STATUS: str = "ok"


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Local app mounting only the health router, bound to the test session."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(health_router, prefix=_API_V1_PREFIX)

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


class TestHealthCheck:
    """The ``GET /health`` DB-connectivity probe (lines 22-23)."""

    async def test_health_ok_when_db_responds(self, client: AsyncClient) -> None:
        """A live DB session yields ``{"status": "ok"}`` with a 200."""
        # Act: hit the health endpoint (its SELECT 1 runs on the test session).
        resp = await client.get(f"{_API_V1_PREFIX}/health")

        # Assert: the probe succeeds and reports the healthy status.
        assert resp.status_code == _HTTP_OK, "health probe returns 200"
        assert resp.json() == {"status": _OK_STATUS}, "healthy status body"

    async def test_health_accepts_head(self, client: AsyncClient) -> None:
        """A ``HEAD`` probe (uptime-monitor default) yields 200, not 405."""
        # Act: hit the endpoint with HEAD (Starlette strips the response body).
        resp = await client.head(f"{_API_V1_PREFIX}/health")

        # Assert: monitors probing with HEAD see a healthy 200.
        assert resp.status_code == _HTTP_OK, "HEAD health probe returns 200"
