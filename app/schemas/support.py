"""Pydantic v2 schemas for the Telegram support helpdesk (support module).

Read models mirror :class:`~app.schemas.order.OrderItemOut` — they use
``model_config = ConfigDict(from_attributes=True)`` so ORM rows can be projected
with ``model_validate``. Internal Telegram identifiers (``tg_chat_id`` /
``tg_message_id``) are deliberately omitted: operators never need them and they
must not leak past the API boundary.

Request bodies: :class:`ReplyIn` (an operator reply, capped at Telegram's text
limit) and :class:`StatusIn` (a conversation status change, validated against
:data:`~app.models.support.SUPPORT_STATUSES`).
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.support import CANNED_LANGS, SUPPORT_STATUSES

# Rendered literal set for the ``status`` field description / error.
_STATUS_CHOICES: str = " | ".join(SUPPORT_STATUSES)
# Rendered literal set for the canned ``lang`` field description / error.
_CANNED_LANG_CHOICES: str = " | ".join(CANNED_LANGS)


class ConversationOut(BaseModel):
    """One support conversation with its inbox metadata (no internal chat id)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_name: str | None
    customer_username: str | None
    lang: str | None
    status: str
    unread_count: int
    last_message_at: datetime
    created_at: datetime


class MessageOut(BaseModel):
    """One message in a conversation (no internal Telegram message id)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    direction: str
    text: str
    delivery: str | None
    sender_staff_id: int | None
    created_at: datetime
    # Attachment kind ("photo"/"document") for a message carrying a file, else
    # None. ``attachment_ready`` is True once the file has been downloaded and
    # stored (its key is set), so the client knows the staff proxy will serve it.
    attachment_kind: str | None = None
    attachment_ready: bool = False


class ConversationList(BaseModel):
    """List envelope ``{data, total, page, page_size}`` for the inbox (§4)."""

    data: list[ConversationOut]
    total: int
    page: int
    page_size: int


class LinkedOrderOut(BaseModel):
    """Summary of the order a conversation is linked to (operator context)."""

    model_config = ConfigDict(from_attributes=True)

    number: str
    status: str
    payment_status: str
    total: Decimal
    created_at: datetime


class ThreadOut(BaseModel):
    """One conversation plus a single page of its messages (§4)."""

    conversation: ConversationOut
    data: list[MessageOut]
    total: int
    page: int
    page_size: int
    linked_order: LinkedOrderOut | None = None


class ReplyIn(BaseModel):
    """Body for ``POST /admin/support/conversations/{id}/reply``."""

    text: str = Field(
        min_length=1,
        max_length=4096,
        description="Operator reply body (Telegram's 4096-char text cap).",
    )


class LinkOrderIn(BaseModel):
    """Body for ``POST /admin/support/conversations/{id}/link``."""

    order_number: str = Field(
        min_length=1,
        max_length=64,
        description="Human-readable order number to link the conversation to.",
    )


class StatusIn(BaseModel):
    """Body for ``POST /admin/support/conversations/{id}/status``."""

    status: str = Field(
        ...,
        description=f"Target conversation status ({_STATUS_CHOICES}).",
    )

    @field_validator("status")
    @classmethod
    def _status_valid(cls, value: str) -> str:
        """Reject any status outside :data:`SUPPORT_STATUSES`.

        Args:
            value: The submitted status.

        Returns:
            str: The validated status.

        Raises:
            ValueError: If the status is not a known value (mapped to 422 by
                FastAPI's request-validation handler).
        """
        if value not in SUPPORT_STATUSES:
            raise ValueError(f"status must be one of {_STATUS_CHOICES}")
        return value


class SupportMetricsPoint(BaseModel):
    """One day bucket of new conversations for the metrics series."""

    day: date
    count: int


class SupportMetricsOut(BaseModel):
    """Support-metrics overview for the admin/director (§ support metrics)."""

    total: int
    new_in_period: int
    open: int
    pending: int
    closed: int
    unanswered: int
    avg_first_response_seconds: float | None
    series: list[SupportMetricsPoint]
    days: int


class CannedOut(BaseModel):
    """One canned reply template (admin picker / management)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    text: str
    lang: str
    sort_order: int
    created_at: datetime


class CannedIn(BaseModel):
    """Body for creating / updating a canned reply template."""

    title: str = Field(
        min_length=1,
        max_length=100,
        description="Short picker label.",
    )
    text: str = Field(
        min_length=1,
        max_length=4096,
        description="Reply body (Telegram's 4096-char text cap).",
    )
    lang: str = Field(
        ...,
        description=f"Template language ({_CANNED_LANG_CHOICES}).",
    )
    sort_order: int = 0

    @field_validator("lang")
    @classmethod
    def _lang_valid(cls, value: str) -> str:
        """Reject any language outside :data:`CANNED_LANGS`.

        Args:
            value: The submitted language.

        Returns:
            str: The validated language.

        Raises:
            ValueError: If the language is not a known value (mapped to 422 by
                FastAPI's request-validation handler).
        """
        if value not in CANNED_LANGS:
            raise ValueError(f"lang must be one of {_CANNED_LANG_CHOICES}")
        return value
