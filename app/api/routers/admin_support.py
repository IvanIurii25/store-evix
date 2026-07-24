"""Admin support inbox API — conversations / thread / reply / status / stream.

Thin back-office router, entirely behind ``Depends(current_staff)`` (a non-staff
caller gets 403 from the dependency). It delegates to
:class:`~app.services.support_service.SupportService`, serializes results into
the shared :mod:`app.schemas.support` DTOs, and maps
:class:`~app.services.support_service.ConversationNotFoundError` to 404 (mirrors
how :mod:`app.api.routers.admin_orders` maps ``OrderNotFoundError``). No business
logic and no SQL live here.

Endpoints (paths carry ``/admin/support``; the ``/api/v1`` prefix is added by the
integrator when mounting):

* ``GET  /admin/support/conversations?status=&page=`` — filtered inbox page.
* ``GET  /admin/support/conversations/{id}?page=`` — the conversation + one page
  of its messages (opening it clears the unread counter).
* ``POST /admin/support/conversations/{id}/reply`` — send an operator reply.
* ``POST /admin/support/conversations/{id}/status`` — change the status.
* ``GET  /admin/support/conversations/stream`` — Server-Sent Events live feed.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.core.db import get_session
from app.core.redis import get_redis
from app.core.support_events import subscribe_support_events
from app.models.user import AppUser
from app.schemas.support import (
    ConversationList,
    ConversationOut,
    MessageOut,
    ReplyIn,
    StatusIn,
    ThreadOut,
)
from app.services.support_service import ConversationNotFoundError, SupportService

router = APIRouter(
    prefix="/admin/support",
    tags=["admin-support"],
    dependencies=[Depends(current_staff)],
)

# Page sizes for the inbox list and a conversation's message thread (§4).
DEFAULT_PAGE_SIZE: int = 20
THREAD_PAGE_SIZE: int = 50


@router.get("/conversations", response_model=ConversationList)
async def list_conversations(
    status: str | None = Query(
        default=None,
        description="Exact conversation-status filter.",
    ),
    page: int = Query(default=1, ge=1, description="1-based page number."),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> ConversationList:
    """Return a filtered, paginated page of inbox conversations.

    Args:
        status: Optional exact conversation-status filter.
        page: 1-based page number.
        session: Injected async DB session.
        redis: Injected async Redis client (required by the service ctor).

    Returns:
        ConversationList: The ``{data, total, page, page_size}`` envelope.
    """
    svc = SupportService(session, redis)
    offset = (page - 1) * DEFAULT_PAGE_SIZE
    rows = await svc.list_conversations(
        status=status,
        offset=offset,
        limit=DEFAULT_PAGE_SIZE,
    )
    total = await svc.count_conversations(status=status)
    return ConversationList(
        data=[ConversationOut.model_validate(c) for c in rows],
        total=total,
        page=page,
        page_size=DEFAULT_PAGE_SIZE,
    )


@router.get("/conversations/stream")
async def stream(redis: Redis = Depends(get_redis)) -> StreamingResponse:
    """Stream live conversation-updated events over Server-Sent Events.

    The admin inbox subscribes here (via ``EventSource``) and refetches on each
    event ``{conversation_id, kind}`` so the list/thread update without polling.
    Auth is the router's ``current_staff`` dependency (cookie-based, works with
    ``EventSource`` same-origin credentials). The stream opens with an SSE
    comment so proxies flush headers immediately.

    Args:
        redis: Injected async Redis client (the pub/sub source).

    Returns:
        StreamingResponse: A ``text/event-stream`` of support events.
    """

    async def _event_gen(redis: Redis):
        """Yield the open comment, then each support event as an SSE frame."""
        yield ": connected\n\n"
        async for event in subscribe_support_events(redis):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        _event_gen(redis),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/conversations/{conversation_id}", response_model=ThreadOut)
async def get_conversation(
    conversation_id: int,
    page: int = Query(default=1, ge=1, description="1-based page number."),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> ThreadOut:
    """Return one conversation with a page of its messages, marking it read.

    Opening a conversation clears its unread counter (``mark_read`` commits);
    the conversation is re-read afterwards so ``unread_count`` in the response is
    already ``0``.

    Args:
        conversation_id: The conversation's primary key.
        page: 1-based page number over the message thread.
        session: Injected async DB session.
        redis: Injected async Redis client (required by the service ctor).

    Returns:
        ThreadOut: The conversation plus one page of its messages.

    Raises:
        HTTPException: 404 if the conversation does not exist.
    """
    svc = SupportService(session, redis)
    conv = await svc.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Conversation not found"},
        )

    await svc.mark_read(conversation_id)
    conv = await svc.get_conversation(conversation_id)

    offset = (page - 1) * THREAD_PAGE_SIZE
    messages = await svc.get_thread(
        conversation_id,
        offset=offset,
        limit=THREAD_PAGE_SIZE,
    )
    total = await svc.count_thread(conversation_id)
    return ThreadOut(
        conversation=ConversationOut.model_validate(conv),
        data=[MessageOut.model_validate(m) for m in messages],
        total=total,
        page=page,
        page_size=THREAD_PAGE_SIZE,
    )


@router.post("/conversations/{conversation_id}/reply", response_model=MessageOut)
async def reply(
    conversation_id: int,
    payload: ReplyIn,
    staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> MessageOut:
    """Send an operator reply into a conversation.

    Args:
        conversation_id: The conversation to reply in.
        payload: The reply body.
        staff: The authenticated staff user (recorded as the sender).
        session: Injected async DB session.
        redis: Injected async Redis client (live-event publishing).

    Returns:
        MessageOut: The persisted outbound message (with its delivery badge).

    Raises:
        HTTPException: 404 if the conversation does not exist.
    """
    svc = SupportService(session, redis)
    try:
        msg = await svc.reply(conversation_id, staff.id, payload.text)
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    # ``created_at`` is a DB ``server_default`` not populated on the instance
    # after commit — refresh so the serialized message carries a timestamp.
    await session.refresh(msg)
    return MessageOut.model_validate(msg)


@router.post("/conversations/{conversation_id}/status", response_model=ConversationOut)
async def set_status(
    conversation_id: int,
    payload: StatusIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> ConversationOut:
    """Change a conversation's status.

    Args:
        conversation_id: The conversation to update.
        payload: The target status (validated against ``SUPPORT_STATUSES``).
        session: Injected async DB session.
        redis: Injected async Redis client (live-event publishing).

    Returns:
        ConversationOut: The updated conversation.

    Raises:
        HTTPException: 404 if the conversation does not exist.
    """
    svc = SupportService(session, redis)
    try:
        conv = await svc.set_status(conversation_id, payload.status)
    except ConversationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return ConversationOut.model_validate(conv)
