"""Redis pub/sub bridge for the support live inbox.

Outbound/inbound/status changes on a conversation are published to a single
Redis channel; the admin SSE endpoint (added later) subscribes and streams them
to the browser so the inbox updates without polling. The Redis client from
:mod:`app.core.redis` is created with ``decode_responses=True``, so the pub/sub
payload arrives as a ``str`` ready for :func:`json.loads`.
"""

import json
from collections.abc import AsyncGenerator

from redis.asyncio import Redis

SUPPORT_EVENTS_CHANNEL: str = "support:events"


async def publish_support_event(
    redis: Redis,
    conversation_id: int,
    kind: str,
) -> None:
    """Publish one support event to the shared channel.

    Args:
        redis: The shared async Redis client.
        conversation_id: The conversation the event is about.
        kind: The event kind — one of ``"inbound"``/``"outbound"``/``"status"``.
    """
    await redis.publish(
        SUPPORT_EVENTS_CHANNEL,
        json.dumps({"conversation_id": conversation_id, "kind": kind}),
    )


async def subscribe_support_events(
    redis: Redis,
) -> AsyncGenerator[dict, None]:
    """Yield decoded support events as they arrive on the channel.

    Consumed by the SSE endpoint: subscribes to the channel and yields each
    event dict until the caller stops iterating, unsubscribing and closing the
    pub/sub connection on teardown.

    Args:
        redis: The shared async Redis client.

    Yields:
        dict: The decoded event (``{"conversation_id": int, "kind": str}``).
    """
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(SUPPORT_EVENTS_CHANNEL)
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            yield json.loads(message["data"])
    finally:
        await pubsub.unsubscribe(SUPPORT_EVENTS_CHANNEL)
        await pubsub.aclose()
