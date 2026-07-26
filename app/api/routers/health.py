"""Health-check router mounted under ``/api/v1``."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session

router = APIRouter(tags=["health"])


@router.api_route("/health", methods=["GET", "HEAD"])
async def health(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    """Report service health and verify DB connectivity with ``SELECT 1``.

    Accepts ``HEAD`` as well as ``GET`` so uptime monitors probing with the
    default ``HEAD`` method get a ``200`` (Starlette strips the body) rather than
    a ``405``.

    Args:
        session: Injected async DB session.

    Returns:
        dict[str, str]: ``{"status": "ok"}`` when the service and DB respond.
    """
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}
