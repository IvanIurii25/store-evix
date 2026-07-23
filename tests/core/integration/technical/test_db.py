"""Unit tests for the session dependency in :mod:`app.core.db`.

``get_session`` is an async generator dependency over the real engine (the dev
DB is up); the test drives it manually to confirm it yields a live, usable
:class:`AsyncSession` and closes cleanly.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session


class TestDbGetSession:
    """``get_session`` yields a usable session and finalizes cleanly."""

    async def test_get_session_yields_async_session(self):
        # Arrange: obtain the generator dependency.
        agen = get_session()

        # Act: advance to the single yielded session, then drive it to close.
        session = await agen.__anext__()
        try:
            # Assert: a live AsyncSession is yielded and can run a query.
            assert isinstance(session, AsyncSession), "must yield an AsyncSession"
            result = await session.scalar(text("SELECT 1"))
            assert result == 1, "the yielded session must run a trivial query"
        finally:
            await agen.aclose()

    async def test_get_session_iterates_exactly_once(self):
        # Arrange / Act: collect everything the generator yields via ``async for``.
        yielded = [session async for session in get_session()]

        # Assert: the dependency yields a single session then stops.
        assert len(yielded) == 1, "get_session must yield exactly one session"
        assert isinstance(yielded[0], AsyncSession), "the yielded value is a session"
