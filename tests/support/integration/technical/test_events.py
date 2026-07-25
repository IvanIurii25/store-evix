"""Unit tests for the Redis pub/sub bridge of the support live inbox.

Exercises :func:`publish_support_event` + :func:`subscribe_support_events` as a
round-trip against an isolated Redis db (15): a subscriber task waits on the
channel, one event is published, and the decoded ``{"conversation_id", "kind"}``
dict must arrive. A short ``asyncio.wait_for`` timeout keeps a broken subscribe
failing fast instead of hanging.
"""

import asyncio

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.core.support_events import (
    publish_support_event,
    subscribe_support_events,
)

pytestmark = pytest.mark.asyncio

# Isolated Redis db for the pub/sub round-trip.
_REDIS_URL: str = "redis://localhost:56379/15"
# Fixed event payload under test.
_CONVERSATION_ID: int = 42
_KIND: str = "inbound"
# Bound the wait so a broken subscribe fails fast rather than hanging the suite.
_SUBSCRIBE_TIMEOUT_S: float = 3.0
# Poll interval while waiting for the subscription to be active before publish.
_READY_POLL_S: float = 0.02
_READY_ATTEMPTS: int = 100


@pytest_asyncio.fixture
async def redis_client() -> Redis:
    """Yield an isolated decode-responses Redis client, flushed on setup."""
    client = Redis.from_url(_REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


async def test_publish_subscribe_round_trip(redis_client: Redis) -> None:
    # Arrange: a subscriber generator + a task that pulls exactly one event.
    events = subscribe_support_events(redis_client)
    received: dict = {}

    async def _consume() -> None:
        received.update(await events.__anext__())

    consumer = asyncio.create_task(_consume())

    # Wait until the channel actually has a subscriber, else the publish races
    # ahead of the SUBSCRIBE and is dropped (pub/sub delivers to live subs only).
    for _ in range(_READY_ATTEMPTS):
        if (await redis_client.pubsub_numsub("support:events"))[0][1] >= 1:
            break
        await asyncio.sleep(_READY_POLL_S)

    # Act: publish one event and await its delivery (bounded).
    await publish_support_event(redis_client, _CONVERSATION_ID, _KIND)
    await asyncio.wait_for(consumer, timeout=_SUBSCRIBE_TIMEOUT_S)

    # Assert: the decoded event carries the published fields.
    assert received == {
        "conversation_id": _CONVERSATION_ID,
        "kind": _KIND,
    }, "the subscriber must decode the exact published event"

    # Cleanup: close the generator so its finally-block unsubscribes/closes.
    await events.aclose()
