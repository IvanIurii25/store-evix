"""Integration tests for the customer-attachment support flow (service + task).

Covers the three layers behind an inbound photo/document, each against the real
session + isolated Redis, with the *local* storage backend redirected under a
per-test ``tmp_path`` so stored bytes are observable and isolated:

* :meth:`SupportService.handle_inbound` — the attachment branch: a photo/document
  ``InboundMessage`` stores a message row (attachment metadata + a placeholder
  ``text`` when the caption is empty, ``attachment_key`` still NULL) and returns
  an :class:`InboundOutcome` carrying the :class:`AttachmentRef` to fetch;
  :meth:`SupportService.set_attachment_key` fills the key afterwards.
* :func:`app.tasks.support._fetch_attachment` — the async fetch helper: with the
  Telegram download monkeypatched, it stores the bytes under a per-conversation
  key and records that key on the message row (streamable via the staff proxy).
* :meth:`SupportService.delete_conversation` / :meth:`SupportService.purge_stale`
  — the erasure paths also remove the stored attachment objects (LP195/2024
  Art.17 on-request erasure / Art.5 storage limitation), leaving no orphaned
  customer files behind.

Storage isolation: every ``get_storage()`` call reads the process-wide
``settings`` (``storage_backend='local'`` → ``LocalStorage`` under
``settings.media_root``). The ``local_storage`` fixture monkeypatches those two
attributes onto the shared settings object so all call sites (service, task,
proxy) share one throwaway media root that pytest cleans up.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.storage as storage_module
import app.services.support_service as support_service_module
import app.tasks.support as support_task
from app.core.storage import get_storage, support_attachment_key
from app.core.telegram import InboundMessage
from app.repositories.support_repo import SupportRepository
from app.services.support_service import AttachmentRef, SupportService

pytestmark = pytest.mark.asyncio

# Telegram chat ids for the scenarios (kept small for readable asserts).
_CHAT_PHOTO: int = 5101
_CHAT_CAPTION: int = 5102
_CHAT_DOC: int = 5103
_CHAT_KEY: int = 5104
_CHAT_FETCH: int = 5105
_CHAT_ERASE: int = 5106
_CHAT_STALE: int = 5107
# Telegram message id + file id used across the inbound builders.
_MSG_ID: int = 30
_FILE_ID: str = "AgACAgIAAxk-file"
# A caption riding along with an attachment.
_CAPTION: str = "смотрите фото"
# The placeholders the service records when an attachment has no caption.
_PHOTO_PLACEHOLDER: str = "📷 Фото"
_DOC_NAME: str = "invoice.pdf"
_DOC_PLACEHOLDER: str = f"📎 {_DOC_NAME}"
# Retention window + ages for the purge test (past vs within the window).
_RETENTION_DAYS: int = 30
_OLD_AGE_DAYS: int = 40
# Bytes the monkeypatched Telegram download returns for the fetch-task test.
_DOWNLOAD_BYTES: bytes = b"\xff\xd8\xffdownloaded-image"
_DOWNLOAD_EXT: str = ".jpg"


def _photo_inbound(*, caption: str = "") -> InboundMessage:
    """Return a parsed photo ``InboundMessage`` (text = caption, may be empty)."""
    return InboundMessage(
        chat_id=_CHAT_PHOTO if caption == "" else _CHAT_CAPTION,
        message_id=_MSG_ID,
        text=caption,
        customer_name="Ana",
        customer_username="ana",
        lang="ro",
        attachment_file_id=_FILE_ID,
        attachment_kind="photo",
        attachment_name=None,
    )


@pytest_asyncio.fixture
async def local_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Redirect the process-wide local storage backend under ``tmp_path``.

    ``get_storage()`` (called with no config by the service/task/proxy) reads the
    shared ``app.core.storage.settings``; pointing its ``media_root`` at a
    throwaway dir isolates every attachment object write to this test.

    Returns:
        Path: The temporary media root the ``LocalStorage`` backend writes under.
    """
    monkeypatch.setattr(storage_module.settings, "storage_backend", "local")
    monkeypatch.setattr(storage_module.settings, "media_root", str(tmp_path))
    return tmp_path


async def _seed_conversation(
    session: AsyncSession,
    *,
    tg_chat_id: int,
    age_days: int | None = None,
) -> int:
    """Persist a conversation, optionally aging ``last_message_at``, return its id."""
    repo = SupportRepository(session)
    conv = await repo.create_conversation(
        tg_chat_id=tg_chat_id,
        customer_name="Ana",
        customer_username="ana",
        lang="ro",
    )
    if age_days is not None:
        conv.last_message_at = datetime.now(UTC) - timedelta(days=age_days)
    await session.flush()
    return conv.id


class SupportServiceHandleAttachmentTest:
    """``handle_inbound`` attachment branch + ``set_attachment_key``."""

    async def test_photo_without_caption_stores_placeholder_and_returns_ref(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a fresh chat sends a captionless photo.
        service = SupportService(db_session, redis_client)

        # Act: ingest the photo inbound.
        outcome = await service.handle_inbound(_photo_inbound())

        # Assert: an AttachmentRef comes back for the fetch task, wired to the
        # stored message + conversation + Telegram file, kind "photo".
        conv = await SupportRepository(db_session).get_by_tg_chat_id(_CHAT_PHOTO)
        assert isinstance(
            outcome.attachment, AttachmentRef
        ), "a photo inbound must return an AttachmentRef to fetch"
        ref = outcome.attachment
        assert ref.conversation_id == conv.id, "ref points at the conversation"
        assert ref.file_id == _FILE_ID, "ref carries the Telegram file id"
        assert ref.kind == "photo", "ref carries the photo kind"

        # And the stored message row carries the placeholder text + metadata, key NULL.
        msg = await SupportRepository(db_session).get_message(ref.message_id)
        assert msg is not None, "the attachment message row must be stored"
        assert msg.attachment_kind == "photo", "photo kind persisted"
        assert msg.text == _PHOTO_PLACEHOLDER, "captionless photo → placeholder text"
        assert msg.attachment_key is None, "the object key stays NULL until fetched"

        # And a fresh burst pings staff with the placeholder as the snippet.
        assert (
            outcome.conversation_id == conv.id
        ), "a brand-new conversation is a fresh burst → its id is returned"
        assert (
            outcome.notify_snippet == _PHOTO_PLACEHOLDER
        ), "the ping snippet is the placeholder"

    async def test_photo_with_caption_stores_caption_as_text(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a photo carrying a caption.
        service = SupportService(db_session, redis_client)

        # Act
        outcome = await service.handle_inbound(_photo_inbound(caption=_CAPTION))

        # Assert: the caption is stored as the message text (no placeholder).
        msg = await SupportRepository(db_session).get_message(
            outcome.attachment.message_id
        )
        assert msg is not None
        assert msg.text == _CAPTION, "a caption becomes the message text"
        assert outcome.notify_snippet == _CAPTION, "the ping snippet is the caption"

    async def test_document_without_caption_uses_named_placeholder(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a captionless document carrying its original filename.
        inbound = InboundMessage(
            chat_id=_CHAT_DOC,
            message_id=_MSG_ID,
            text="",
            customer_name="Ana",
            customer_username="ana",
            lang="ro",
            attachment_file_id=_FILE_ID,
            attachment_kind="document",
            attachment_name=_DOC_NAME,
        )
        service = SupportService(db_session, redis_client)

        # Act
        outcome = await service.handle_inbound(inbound)

        # Assert: document kind + filename persisted, "📎 <name>" placeholder text.
        msg = await SupportRepository(db_session).get_message(
            outcome.attachment.message_id
        )
        assert msg is not None
        assert msg.attachment_kind == "document", "document kind persisted"
        assert msg.attachment_name == _DOC_NAME, "the original filename persisted"
        assert msg.text == _DOC_PLACEHOLDER, "captionless document → named placeholder"

    async def test_set_attachment_key_records_the_key(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a stored photo message with a NULL key.
        service = SupportService(db_session, redis_client)
        inbound = _photo_inbound()
        inbound = InboundMessage(
            chat_id=_CHAT_KEY,
            message_id=_MSG_ID,
            text="",
            customer_name="Ana",
            customer_username="ana",
            lang="ro",
            attachment_file_id=_FILE_ID,
            attachment_kind="photo",
            attachment_name=None,
        )
        outcome = await service.handle_inbound(inbound)
        message_id = outcome.attachment.message_id
        key = support_attachment_key(outcome.attachment.conversation_id, ".jpg")

        # Act: the fetch task's callback records the stored object key.
        await service.set_attachment_key(message_id, key)

        # Assert: a fresh read shows the key set on the message row.
        db_session.expire_all()
        msg = await SupportRepository(db_session).get_message(message_id)
        assert msg is not None
        assert msg.attachment_key == key, "set_attachment_key must persist the key"

    async def test_set_attachment_key_missing_message_is_noop(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
    ) -> None:
        # Arrange: a service over an inbox with no such message.
        service = SupportService(db_session, redis_client)

        # Act / Assert: setting the key on a missing message must not raise.
        await service.set_attachment_key(999999, "support/1/deadbeef.jpg")


class SupportFetchAttachmentTaskTest:
    """``app.tasks.support._fetch_attachment`` — download → store → record key."""

    async def test_fetch_stores_bytes_and_records_key(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
        local_storage: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: a stored photo message (key NULL) and a monkeypatched Telegram
        # download returning fixed bytes + extension. Storage is the real
        # LocalStorage under tmp_path (via the local_storage fixture).
        service = SupportService(db_session, redis_client)
        inbound = InboundMessage(
            chat_id=_CHAT_FETCH,
            message_id=_MSG_ID,
            text="",
            customer_name="Ana",
            customer_username="ana",
            lang="ro",
            attachment_file_id=_FILE_ID,
            attachment_kind="photo",
            attachment_name=None,
        )
        outcome = await service.handle_inbound(inbound)
        ref = outcome.attachment

        async def _fake_download(file_id: str) -> tuple[bytes, str]:
            return _DOWNLOAD_BYTES, _DOWNLOAD_EXT

        monkeypatch.setattr(support_task, "download_file_isolated", _fake_download)

        # Act: run the async fetch helper directly against the test session.
        await support_task._fetch_attachment(
            db_session,
            message_id=ref.message_id,
            conversation_id=ref.conversation_id,
            file_id=ref.file_id,
        )

        # Assert: the message now carries a key, and the stored object is fetchable
        # via the same LocalStorage the proxy would read.
        db_session.expire_all()
        msg = await SupportRepository(db_session).get_message(ref.message_id)
        assert msg is not None
        assert msg.attachment_key is not None, "the fetch task must record the key"
        fetched = await get_storage().fetch_key(msg.attachment_key)
        assert fetched is not None, "the stored object must be fetchable"
        data, _content_type = fetched
        assert data == _DOWNLOAD_BYTES, "the stored bytes are the downloaded bytes"

    async def test_fetch_empty_token_is_noop(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
        local_storage: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: a stored photo message and a download that reports the dev
        # no-op (empty token → download_file_isolated returns None).
        service = SupportService(db_session, redis_client)
        inbound = InboundMessage(
            chat_id=_CHAT_FETCH,
            message_id=_MSG_ID + 1,
            text="",
            customer_name="Ana",
            customer_username="ana",
            lang="ro",
            attachment_file_id=_FILE_ID,
            attachment_kind="photo",
            attachment_name=None,
        )
        outcome = await service.handle_inbound(inbound)
        ref = outcome.attachment

        async def _no_download(file_id: str) -> None:
            return None

        monkeypatch.setattr(support_task, "download_file_isolated", _no_download)

        # Act: the helper must short-circuit without touching storage or the row.
        await support_task._fetch_attachment(
            db_session,
            message_id=ref.message_id,
            conversation_id=ref.conversation_id,
            file_id=ref.file_id,
        )

        # Assert: the key stays NULL (nothing downloaded, nothing stored).
        db_session.expire_all()
        msg = await SupportRepository(db_session).get_message(ref.message_id)
        assert msg is not None
        assert (
            msg.attachment_key is None
        ), "an empty-token no-op must leave the key unset"

    async def test_fetch_missing_message_stores_object_without_raising(
        self,
        db_session: AsyncSession,
        local_storage: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: a download that succeeds, but the target message row is gone
        # (e.g. the conversation was erased between enqueue and run).
        async def _fake_download(file_id: str) -> tuple[bytes, str]:
            return _DOWNLOAD_BYTES, _DOWNLOAD_EXT

        monkeypatch.setattr(support_task, "download_file_isolated", _fake_download)

        # Act: the helper must store the bytes and then no-op on the missing row.
        await support_task._fetch_attachment(
            db_session,
            message_id=999999,
            conversation_id=_CHAT_FETCH,
            file_id=_FILE_ID,
        )

        # Assert: no row to update, and the call did not raise (the guard held).
        assert (
            await SupportRepository(db_session).get_message(999999) is None
        ), "there is no message row to record a key on"


class SupportAttachmentErasureTest:
    """Erasure paths remove the stored attachment objects, not just the rows."""

    async def _store_attachment(
        self,
        session: AsyncSession,
        conversation_id: int,
    ) -> tuple[int, str]:
        """Store an object + a message row referencing its key; return (msg_id, key).

        Args:
            session: The active test session.
            conversation_id: The conversation the attachment message belongs to.

        Returns:
            tuple[int, str]: The attachment message id and the stored object key.
        """
        key = support_attachment_key(conversation_id, ".jpg")
        await get_storage().put_key(key, b"IMG", content_type="image/jpeg")
        repo = SupportRepository(session)
        msg = await repo.add_message(
            conversation_id=conversation_id,
            direction="in",
            text=_PHOTO_PLACEHOLDER,
            tg_message_id=_MSG_ID,
            attachment_kind="photo",
        )
        msg.attachment_key = key
        await session.flush()
        return msg.id, key

    async def test_delete_conversation_removes_attachment_object(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
        local_storage: Path,
    ) -> None:
        # Arrange: a conversation whose message has a stored attachment object.
        conv_id = await _seed_conversation(db_session, tg_chat_id=_CHAT_ERASE)
        _msg_id, key = await self._store_attachment(db_session, conv_id)
        assert await get_storage().fetch_key(key) is not None, "object present first"
        service = SupportService(db_session, redis_client)

        # Act: erase the conversation on request.
        await service.delete_conversation(conv_id)

        # Assert: both the rows and the stored object are gone.
        db_session.expire_all()
        assert (
            await SupportRepository(db_session).get_conversation(conv_id) is None
        ), "the conversation row must be deleted"
        assert (
            await get_storage().fetch_key(key) is None
        ), "the stored attachment object must be removed on erasure"

    async def test_delete_conversation_survives_storage_removal_failure(
        self,
        db_session: AsyncSession,
        redis_client: Redis,
        local_storage: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: a conversation with a stored attachment, and a storage whose
        # remove_key raises — erasure must never be blocked by a storage failure.
        conv_id = await _seed_conversation(db_session, tg_chat_id=_CHAT_ERASE)
        await self._store_attachment(db_session, conv_id)
        failing_storage = AsyncMock()
        failing_storage.remove_key = AsyncMock(side_effect=RuntimeError("storage down"))
        monkeypatch.setattr(
            support_service_module, "get_storage", lambda: failing_storage
        )
        service = SupportService(db_session, redis_client)

        # Act: erase the conversation while object removal fails.
        await service.delete_conversation(conv_id)

        # Assert: the rows are still deleted (the failure was logged, not raised),
        # and the removal was at least attempted.
        db_session.expire_all()
        assert (
            await SupportRepository(db_session).get_conversation(conv_id) is None
        ), "a storage removal failure must not block row erasure"
        failing_storage.remove_key.assert_awaited_once()

    async def test_purge_stale_removes_attachment_object(
        self,
        db_session: AsyncSession,
        local_storage: Path,
    ) -> None:
        # Arrange: a stale conversation (past the window) with a stored attachment.
        conv_id = await _seed_conversation(
            db_session, tg_chat_id=_CHAT_STALE, age_days=_OLD_AGE_DAYS
        )
        _msg_id, key = await self._store_attachment(db_session, conv_id)
        assert await get_storage().fetch_key(key) is not None, "object present first"
        # The batch purge path is built without Redis (mirrors the Celery task).
        service = SupportService(db_session)

        # Act: sweep conversations inactive past the retention window.
        deleted = await service.purge_stale(_RETENTION_DAYS)

        # Assert: the stale conversation was purged and its object removed.
        assert deleted == 1, "exactly one conversation is past the retention window"
        db_session.expire_all()
        assert (
            await SupportRepository(db_session).get_conversation(conv_id) is None
        ), "the stale conversation must be purged"
        assert (
            await get_storage().fetch_key(key) is None
        ), "the stale conversation's attachment object must be removed"
