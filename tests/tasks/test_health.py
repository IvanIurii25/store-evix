"""Tests for the Celery health tasks and the async-DB bridge.

``ping`` is exercised in eager mode (no broker/worker needed). ``db_check``'s
async impl is awaited directly against the shared test session — the
``asyncio.run`` wrapper in ``run_async_session`` cannot run inside pytest's
already-running loop and is thin infra verified at deploy instead.
"""

import pytest

from app.core.celery_app import celery_app
from app.tasks.health import _db_check, ping


@pytest.fixture
def eager_celery():
    """Run tasks synchronously in-process and surface task exceptions.

    ``task_always_eager`` executes ``.delay()`` inline (no broker/worker);
    ``task_eager_propagates`` re-raises task errors into ``.get()``.
    """
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    try:
        yield
    finally:
        celery_app.conf.task_always_eager = False
        celery_app.conf.task_eager_propagates = False


def test_ping_eager(eager_celery) -> None:
    """``ping`` returns ``"pong"`` in eager mode without a broker or loop."""
    assert ping.delay().get() == "pong"


async def test_db_check_impl(db_session) -> None:
    """The async ``_db_check`` impl returns ``1`` against the test session."""
    assert await _db_check(db_session) == 1
