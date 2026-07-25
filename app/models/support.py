"""Telegram-based helpdesk domain models (support module).

A customer talks to the store over a Telegram bot; operators reply from the
admin inbox. Each Telegram chat maps to exactly one :class:`SupportConversation`
(the running thread + inbox metadata), and every individual message — inbound
from the customer or outbound from an operator — is an append-only
:class:`SupportMessage` row. Conversations are updated in place (status /
unread_count / last_message_at), messages are never mutated after insert.

Telegram chat/message ids can exceed int32, so those columns use ``BigInteger``.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Conversation lifecycle: open (needs attention) → pending (operator waiting on
# customer) → closed (resolved). Directions distinguish who sent a message;
# delivery records the send result of outbound messages only.
SUPPORT_STATUSES: tuple[str, ...] = ("open", "pending", "closed")
SUPPORT_DIRECTIONS: tuple[str, ...] = (
    "in",
    "out",
)  # in = from customer, out = from operator
SUPPORT_DELIVERY: tuple[str, ...] = ("sent", "failed")  # outbound send result
# Inbound messages may carry an attachment; photos are inline images, documents
# keep their original filename. NULL kind = a plain text message.
ATTACHMENT_KINDS: tuple[str, ...] = ("photo", "document")
# Canned reply templates are authored per storefront language (ro/ru).
CANNED_LANGS: tuple[str, ...] = ("ro", "ru")
_STATUS_IN = ", ".join(f"'{s}'" for s in SUPPORT_STATUSES)
_DIRECTION_IN = ", ".join(f"'{d}'" for d in SUPPORT_DIRECTIONS)
_DELIVERY_IN = ", ".join(f"'{d}'" for d in SUPPORT_DELIVERY)
_ATTACHMENT_KIND_IN = ", ".join(f"'{k}'" for k in ATTACHMENT_KINDS)
_CANNED_LANG_IN = ", ".join(f"'{lang}'" for lang in CANNED_LANGS)


class SupportConversation(Base):
    """One Telegram chat = one support thread plus its inbox metadata."""

    __tablename__ = "support_conversation"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_IN})", name="support_status_valid"),
        # Inbox list: filter by status, order by most-recent activity.
        Index("ix_support_conversation_status_last", "status", "last_message_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # The Telegram chat id — one conversation per chat, so unique.
    tg_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    # Optional link to an order so the operator sees the order context. Set
    # manually by an operator or auto-populated from an ``o<number>`` deep-link.
    # ON DELETE SET NULL so the conversation survives the order being removed.
    linked_order_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("order.id", ondelete="SET NULL"),
        nullable=True,
    )
    # First+last name snapshot from Telegram (best-effort, may drift).
    customer_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # @username snapshot; Telegram users may have no username.
    customer_username: Mapped[str | None] = mapped_column(String, nullable=True)
    # Telegram language_code (ru/ro/…), best-effort.
    lang: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        server_default=text("'open'"),
    )
    # Inbound messages the operators haven't seen yet.
    unread_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("'0'"),
    )
    # Bumped on every message; drives inbox ordering.
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class SupportMessage(Base):
    """One immutable message in a conversation (append-only thread)."""

    __tablename__ = "support_message"
    __table_args__ = (
        CheckConstraint(
            f"direction IN ({_DIRECTION_IN})", name="support_direction_valid"
        ),
        CheckConstraint(
            f"delivery IS NULL OR delivery IN ({_DELIVERY_IN})",
            name="support_delivery_valid",
        ),
        CheckConstraint(
            f"attachment_kind IS NULL OR attachment_kind IN ({_ATTACHMENT_KIND_IN})",
            name="support_attachment_kind_valid",
        ),
        # Thread render: all messages of a conversation in chronological order.
        Index(
            "ix_support_message_conversation_created", "conversation_id", "created_at"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("support_conversation.id", ondelete="CASCADE"),
        nullable=False,
    )
    direction: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str] = mapped_column(String, nullable=False)
    # Telegram message id: set for inbound immediately, for outbound after send.
    # Used to dedup inbound updates.
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Operator who sent an outbound message; null for inbound. ON DELETE SET NULL
    # so the thread survives an operator account being removed.
    sender_staff_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("app_user.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Outbound send result ("sent"/"failed"); null for inbound.
    delivery: Mapped[str | None] = mapped_column(String, nullable=True)
    # Attachment kind ("photo"/"document") for inbound messages carrying a file;
    # null for plain-text messages. When set, ``text`` holds the caption (or a
    # placeholder like "[фото]").
    attachment_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    # S3/local object key of the downloaded attachment (see
    # ``support_attachment_key``); null while the fetch is pending or when there
    # is no attachment. This is what the staff proxy streams and what
    # erasure/retention delete.
    attachment_key: Mapped[str | None] = mapped_column(String, nullable=True)
    # Original filename for documents (display/download); null for photos.
    attachment_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class SupportCannedResponse(Base):
    """A reusable per-language reply template operators insert into a reply."""

    __tablename__ = "support_canned_response"
    __table_args__ = (
        CheckConstraint(f"lang IN ({_CANNED_LANG_IN})", name="canned_lang_valid"),
        # Ordered per-language picker: filter by lang, order by sort_order.
        Index("ix_support_canned_lang_sort", "lang", "sort_order"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Short label shown in the operator's canned-response picker.
    title: Mapped[str] = mapped_column(String, nullable=False)
    # Storefront language the template is authored in (ro/ru).
    lang: Mapped[str] = mapped_column(String, nullable=False)
    # Manual ordering within a language (ascending). Declared before the ``text``
    # column so the ``text()`` construct is not shadowed by that column name.
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("'0'"),
    )
    # The reply body inserted into the reply box.
    text: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
