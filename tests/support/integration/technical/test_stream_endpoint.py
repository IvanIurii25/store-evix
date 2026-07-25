"""Focused test for the admin SSE ``stream`` endpoint generator.

The full HTTP round-trip over httpx's in-process ``ASGITransport`` hangs on
teardown (the endpoint parks in ``pubsub.listen()`` and the streaming task does
not unwind on client close). To still cover the SSE frame-formatting body
(``: connected`` open comment + ``data: {...}`` frames) without that hang, we
call the route coroutine directly and drive its ``StreamingResponse`` body
iterator with bounded reads, publishing one event, then close the iterator so
its ``finally`` unsubscribes.
"""

import asyncio
import json

import pytest
from redis.asyncio import Redis

from app.api.routers.admin_support import stream
from app.core.support_events import SUPPORT_EVENTS_CHANNEL, publish_support_event

pytestmark = pytest.mark.asyncio

# Fixed event payload the endpoint must serialize into an SSE ``data:`` frame.
_CONVERSATION_ID = 4242
_KIND = "inbound"
# Bound each read so a broken subscribe fails fast instead of hanging the suite.
_READ_TIMEOUT_S = 3.0
# Poll for an active subscriber before publishing (pub/sub drops to no subs).
_READY_POLL_S = 0.02
_READY_ATTEMPTS = 100


async def test_stream_emits_connected_then_event_frame(redis_client: Redis) -> None:
    """The endpoint yields the open comment, then a ``data:`` frame per event."""
    # Arrange — call the route coroutine to get its StreamingResponse iterator.
    response = await stream(redis=redis_client)
    iterator = response.body_iterator

    try:
        # The first yield is the SSE open comment, emitted before any subscribe
        # blocking, so a single bounded read returns it deterministically.
        first = await asyncio.wait_for(iterator.__anext__(), timeout=_READ_TIMEOUT_S)
        assert first == ": connected\n\n", "stream must open with the SSE comment"

        # Pulling the next frame drives the generator into ``subscribe`` +
        # ``listen`` and blocks there; start it as a task so we can publish once
        # the subscription is actually live (else the publish hits no subscriber).
        next_frame = asyncio.create_task(iterator.__anext__())
        for _ in range(_READY_ATTEMPTS):
            subs = (await redis_client.pubsub_numsub(SUPPORT_EVENTS_CHANNEL))[0][1]
            if subs >= 1:
                break
            await asyncio.sleep(_READY_POLL_S)

        # Act — publish one event; the pending frame read must resolve to it.
        await publish_support_event(redis_client, _CONVERSATION_ID, _KIND)
        frame = await asyncio.wait_for(next_frame, timeout=_READ_TIMEOUT_S)

        # Assert — the frame is ``data: {json}\n\n`` carrying the published event.
        assert frame.startswith("data: "), "event frames use the SSE data: prefix"
        assert frame.endswith("\n\n"), "SSE frames end with a blank line"
        payload = json.loads(frame[len("data: ") : -2])
        assert payload == {
            "conversation_id": _CONVERSATION_ID,
            "kind": _KIND,
        }, "the frame must carry the published event"
    finally:
        # Close the generator so its finally-block unsubscribes/closes the pubsub.
        await iterator.aclose()
