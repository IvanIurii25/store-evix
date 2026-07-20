"""Health-check router mounted under ``/api/v1``."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    """Report service health and verify DB connectivity with ``SELECT 1``.

    Args:
        session: Injected async DB session.

    Returns:
        dict[str, str]: ``{"status": "ok"}`` when the service and DB respond.
    """
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}
