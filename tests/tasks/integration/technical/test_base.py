"""Test for the async-DB bridge :func:`run_async_session` (Phase 1).

``run_async_session`` builds a FRESH ``NullPool`` engine on a fresh event loop
via :func:`asyncio.run`, so it CANNOT run inside pytest-asyncio's already-running
loop — this test is deliberately a **plain synchronous** ``def`` (no
``db_session``, no ``async``). It hits the real dev DB from
``settings.database_url`` (which has the schema) with a trivial ``SELECT 1`` to
prove the engine/session/loop plumbing round-trips a value out.
"""

from sqlalchemy import select

from app.tasks.base import run_async_session

# The scalar the bridged operation must return end-to-end.
_EXPECTED_SCALAR: int = 1


def test_run_async_session_returns_op_result() -> None:
    """The bridge runs the op against a fresh session and returns its value."""

    # Arrange: an async op that selects a constant against the handed session.
    async def _select_one(session) -> int:  # noqa: ANN001 — AsyncSession
        return await session.scalar(select(_EXPECTED_SCALAR))

    # Act: run it through the sync bridge (own engine + own event loop).
    result = run_async_session(_select_one)

    # Assert: the op's return value propagates back out of asyncio.run.
    assert result == _EXPECTED_SCALAR, "bridge must return the op's result"
