"""Admin support inbox API — conversations / thread / reply / status / stream.

Thin back-office router, entirely behind ``Depends(current_staff)`` (a non-staff
caller gets 403 from the dependency). It delegates to
:class:`~app.services.support_service.SupportService` and serializes results into
the shared :mod:`app.schemas.support` DTOs. The service's
:class:`~app.services.support_service.SupportError` subclasses carry their own
``status_code``/``code`` and are rendered by the centralized
:class:`~app.core.errors.DomainError` handler, so no ``try``/``except`` mapping
lives here. No business logic and no SQL live here.

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
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.core.db import get_session
from app.core.redis import get_redis
from app.core.storage import get_storage
from app.core.support_events import subscribe_support_events
from app.models.support import SupportMessage
from app.models.user import AppUser
from app.repositories.support_repo import SupportRepository
from app.schemas.support import (
    CannedIn,
    CannedOut,
    ConversationList,
    ConversationOut,
    LinkedOrderOut,
    LinkOrderIn,
    MessageOut,
    ReplyIn,
    StatusIn,
    SupportMetricsOut,
    SupportMetricsPoint,
    ThreadOut,
)
from app.services.support_service import (
    SupportCannedService,
    SupportService,
)

router = APIRouter(
    prefix="/admin/support",
    tags=["admin-support"],
    dependencies=[Depends(current_staff)],
)

# Page sizes for the inbox list and a conversation's message thread (§4).
DEFAULT_PAGE_SIZE: int = 20
THREAD_PAGE_SIZE: int = 50


def _to_message_out(message: SupportMessage) -> MessageOut:
    """Serialize a message row, flagging whether its attachment is downloaded.

    ``attachment_ready`` is a derived field (not an ORM column): the attachment
    is streamable once its object key is set, which the fetch task fills
    asynchronously after the message row already exists.

    Args:
        message: The message ORM row.

    Returns:
        MessageOut: The serialized message with ``attachment_ready`` derived from
        the presence of a stored object key.
    """
    out = MessageOut.model_validate(message)
    out.attachment_ready = message.attachment_key is not None
    return out


@router.get("/conversations", response_model=ConversationList)
async def list_conversations(
    status: str | None = Query(
        default=None,
        description="Exact conversation-status filter.",
    ),
    page: int = Query(default=1, ge=1, le=10000, description="1-based page number."),
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


@router.get("/metrics", response_model=SupportMetricsOut)
async def metrics(
    days: int = Query(
        default=30,
        ge=1,
        le=365,
        description="Trailing window size in days for the metrics overview.",
    ),
    session: AsyncSession = Depends(get_session),
) -> SupportMetricsOut:
    """Return the support-metrics overview for the admin/director.

    A read-only aggregation over the existing support tables: conversation
    totals and current status split, how many conversations still await an
    operator reply, the average first-response time, and the per-day new-
    conversation series — all scoped to the trailing ``days`` window where
    period-relative.

    Args:
        days: Trailing window size in days (1-365, default 30).
        session: Injected async DB session.

    Returns:
        SupportMetricsOut: The assembled metrics overview.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    repo = SupportRepository(session)
    summary = await repo.metrics_summary(cutoff)
    unanswered = await repo.unanswered_count()
    avg_first_response = await repo.avg_first_response_seconds(cutoff)
    series = await repo.new_conversations_series(cutoff)
    return SupportMetricsOut(
        total=summary["total"],
        new_in_period=summary["new_in_period"],
        open=summary["open"],
        pending=summary["pending"],
        closed=summary["closed"],
        unanswered=unanswered,
        avg_first_response_seconds=avg_first_response,
        series=[SupportMetricsPoint(day=d, count=c) for d, c in series],
        days=days,
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
    page: int = Query(default=1, ge=1, le=10000, description="1-based page number."),
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
    linked = await svc.get_linked_order(conv)
    return ThreadOut(
        conversation=ConversationOut.model_validate(conv),
        data=[_to_message_out(m) for m in messages],
        total=total,
        page=page,
        page_size=THREAD_PAGE_SIZE,
        linked_order=LinkedOrderOut.model_validate(linked) if linked else None,
    )


@router.get("/conversations/{conversation_id}/attachments/{message_id}")
async def get_attachment(
    conversation_id: int,
    message_id: int,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Stream a customer's stored attachment to a staff operator (private proxy).

    Support attachments are never public: they are stored under a private object
    key and streamed only through this staff-only endpoint (the router's
    ``current_staff`` dependency). Resolves the message, verifies it belongs to
    the conversation and has a stored key, fetches the bytes and returns them
    inline; a document keeps its original filename in ``Content-Disposition``.

    Args:
        conversation_id: The conversation the attachment must belong to.
        message_id: The attachment message's primary key.
        session: Injected async DB session.

    Returns:
        Response: The attachment bytes with the stored content type.

    Raises:
        HTTPException: 404 if the message is missing, does not belong to the
            conversation, has no stored attachment, or the object is gone.
    """
    message = await SupportRepository(session).get_message(message_id)
    if (
        message is None
        or message.conversation_id != conversation_id
        or message.attachment_key is None
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Attachment not found"},
        )

    fetched = await get_storage().fetch_key(message.attachment_key)
    if fetched is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "Attachment not found"},
        )

    data, content_type = fetched
    headers: dict[str, str] = {}
    if message.attachment_name:
        headers["Content-Disposition"] = f'inline; filename="{message.attachment_name}"'
    return Response(content=data, media_type=content_type, headers=headers)


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
        ConversationNotFoundError: 404 if the conversation does not exist.
    """
    svc = SupportService(session, redis)
    msg = await svc.reply(conversation_id, staff.id, payload.text)
    # ``created_at`` is a DB ``server_default`` not populated on the instance
    # after commit — refresh so the serialized message carries a timestamp.
    await session.refresh(msg)
    return _to_message_out(msg)


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
        ConversationNotFoundError: 404 if the conversation does not exist.
    """
    svc = SupportService(session, redis)
    conv = await svc.set_status(conversation_id, payload.status)
    return ConversationOut.model_validate(conv)


@router.post("/conversations/{conversation_id}/link", response_model=LinkedOrderOut)
async def link_order(
    conversation_id: int,
    payload: LinkOrderIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> LinkedOrderOut:
    """Link a conversation to an order by its number (operator context).

    Args:
        conversation_id: The conversation to link.
        payload: The order number to resolve and link.
        session: Injected async DB session.
        redis: Injected async Redis client (required by the service ctor).

    Returns:
        LinkedOrderOut: The linked order's summary.

    Raises:
        OrderNotFoundError: 404 if no order matches the number.
        ConversationNotFoundError: 404 if the conversation does not exist.
    """
    svc = SupportService(session, redis)
    order = await svc.link_order(conversation_id, payload.order_number)
    return LinkedOrderOut.model_validate(order)


@router.delete(
    "/conversations/{conversation_id}/link",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unlink_order(
    conversation_id: int,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> None:
    """Clear a conversation's order link.

    Args:
        conversation_id: The conversation to unlink.
        session: Injected async DB session.
        redis: Injected async Redis client (required by the service ctor).

    Raises:
        ConversationNotFoundError: 404 if the conversation does not exist.
    """
    svc = SupportService(session, redis)
    await svc.unlink_order(conversation_id)


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_conversation(
    conversation_id: int,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> None:
    """Hard-delete a conversation and its messages (on-request erasure).

    The on-request erasure path (LP195/2024 Art.17) for Telegram support data,
    which is not account-linked (so account erasure cannot reach it). Deleting
    the conversation cascades to its messages; other operators' live inboxes are
    told to drop it via a published event.

    Args:
        conversation_id: The conversation to erase.
        session: Injected async DB session.
        redis: Injected async Redis client (live-event publishing).

    Raises:
        ConversationNotFoundError: 404 if the conversation does not exist.
    """
    svc = SupportService(session, redis)
    await svc.delete_conversation(conversation_id)


@router.get("/canned", response_model=list[CannedOut])
async def list_canned(
    lang: str | None = Query(
        default=None,
        description="Exact template-language filter (ro/ru).",
    ),
    session: AsyncSession = Depends(get_session),
) -> list[CannedOut]:
    """Return canned reply templates, optionally filtered by language.

    Args:
        lang: Optional exact template-language filter.
        session: Injected async DB session.

    Returns:
        list[CannedOut]: The templates, ordered for the picker.
    """
    rows = await SupportCannedService(session).list_canned(lang)
    return [CannedOut.model_validate(c) for c in rows]


@router.post(
    "/canned",
    response_model=CannedOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_canned(
    payload: CannedIn,
    session: AsyncSession = Depends(get_session),
) -> CannedOut:
    """Create a canned reply template.

    Args:
        payload: The validated template body.
        session: Injected async DB session.

    Returns:
        CannedOut: The persisted template.
    """
    canned = await SupportCannedService(session).create_canned(payload)
    return CannedOut.model_validate(canned)


@router.patch("/canned/{canned_id}", response_model=CannedOut)
async def update_canned(
    canned_id: int,
    payload: CannedIn,
    session: AsyncSession = Depends(get_session),
) -> CannedOut:
    """Update a canned reply template.

    Args:
        canned_id: The template's primary key.
        payload: The validated template body.
        session: Injected async DB session.

    Returns:
        CannedOut: The updated template.

    Raises:
        CannedNotFoundError: 404 if the template does not exist.
    """
    canned = await SupportCannedService(session).update_canned(canned_id, payload)
    return CannedOut.model_validate(canned)


@router.delete("/canned/{canned_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_canned(
    canned_id: int,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a canned reply template.

    Args:
        canned_id: The template's primary key.
        session: Injected async DB session.

    Raises:
        CannedNotFoundError: 404 if the template does not exist.
    """
    await SupportCannedService(session).delete_canned(canned_id)
