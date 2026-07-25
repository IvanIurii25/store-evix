"""Integration tests for the staff attachment proxy + thread ``attachment_ready``.

Drives the staff-only private proxy over HTTP:

* ``GET /conversations/{cid}/attachments/{mid}`` — streams a stored object's bytes
  with its content type once the message's ``attachment_key`` is set; 404s when
  the message has no key, does not belong to the conversation, or the object is
  gone; and the ``current_staff`` guard blocks an unauthenticated caller.
* ``GET /conversations/{id}`` — a photo message's :class:`MessageOut` carries
  ``attachment_kind='photo'`` and ``attachment_ready`` flips ``False`` → ``True``
  once the fetch task records the object key (the ``_to_message_out`` derived
  flag).

Storage isolation: the proxy calls ``get_storage()`` with no config, reading the
process-wide ``settings``; the ``local_storage`` fixture redirects the local
backend's ``media_root`` under a per-test ``tmp_path`` so the object put here is
what the proxy reads and nothing leaks between tests.
"""

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.storage as storage_module
from app.core.storage import get_storage, support_attachment_key
from app.repositories.support_repo import SupportRepository

pytestmark = pytest.mark.asyncio

_BASE: str = "/api/v1/admin/support/conversations"
# Telegram chat ids for the seeded conversations (kept small).
_CHAT_A: int = 6001
_CHAT_B: int = 6002
# The stored attachment's bytes + content type put directly into storage.
_OBJECT_BYTES: bytes = b"BYTES"
_OBJECT_CONTENT_TYPE: str = "image/jpeg"
# A document filename to assert the Content-Disposition path.
_DOC_NAME: str = "invoice.pdf"


@pytest_asyncio.fixture
async def local_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Redirect the process-wide local storage backend under ``tmp_path``.

    The proxy resolves ``get_storage()`` from the shared ``settings``; pointing
    ``media_root`` at a throwaway dir means the object put here is exactly what
    the endpoint streams, isolated per test.

    Returns:
        Path: The temporary media root the local backend writes under.
    """
    monkeypatch.setattr(storage_module.settings, "storage_backend", "local")
    monkeypatch.setattr(storage_module.settings, "media_root", str(tmp_path))
    return tmp_path


async def _seed_attachment_message(
    session: AsyncSession,
    *,
    tg_chat_id: int,
    with_key: bool = True,
    attachment_name: str | None = None,
) -> tuple[int, int, str | None]:
    """Persist a conversation + a photo/document message, optionally storing a key.

    Args:
        session: The active test session.
        tg_chat_id: The Telegram chat id for the conversation.
        with_key: Whether to store an object and set the message's key.
        attachment_name: The document filename (``None`` for a photo).

    Returns:
        tuple[int, int, str | None]: ``(conversation_id, message_id, key)`` — the
        key is ``None`` when ``with_key`` is ``False``.
    """
    repo = SupportRepository(session)
    conv = await repo.create_conversation(
        tg_chat_id=tg_chat_id,
        customer_name="Ana",
        customer_username="ana",
        lang="ro",
    )
    kind = "document" if attachment_name else "photo"
    msg = await repo.add_message(
        conversation_id=conv.id,
        direction="in",
        text="📷 Фото",
        tg_message_id=99,
        attachment_kind=kind,
        attachment_name=attachment_name,
    )
    key: str | None = None
    if with_key:
        key = support_attachment_key(conv.id, ".jpg")
        await get_storage().put_key(
            key, _OBJECT_BYTES, content_type=_OBJECT_CONTENT_TYPE
        )
        msg.attachment_key = key
    await session.flush()
    return conv.id, msg.id, key


class AttachmentProxyTest:
    """``GET /conversations/{cid}/attachments/{mid}`` — stream + 404s + guard."""

    async def test_streams_stored_object_bytes_and_content_type(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        local_storage: Path,
    ) -> None:
        # Arrange: a conversation + a photo message whose object is stored.
        conv_id, msg_id, _key = await _seed_attachment_message(
            db_session, tg_chat_id=_CHAT_A
        )

        # Act: the staff proxy streams the attachment.
        resp = await client.get(f"{_BASE}/{conv_id}/attachments/{msg_id}")

        # Assert: the exact bytes come back with the stored content type.
        assert resp.status_code == 200, resp.text
        assert resp.content == _OBJECT_BYTES, "the proxy streams the stored bytes"
        assert resp.headers["content-type"] == _OBJECT_CONTENT_TYPE, "content type"

    async def test_document_keeps_original_filename(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        local_storage: Path,
    ) -> None:
        # Arrange: a document message carrying its original filename.
        conv_id, msg_id, _key = await _seed_attachment_message(
            db_session, tg_chat_id=_CHAT_A, attachment_name=_DOC_NAME
        )

        # Act
        resp = await client.get(f"{_BASE}/{conv_id}/attachments/{msg_id}")

        # Assert: the original filename rides in Content-Disposition.
        assert resp.status_code == 200, resp.text
        assert (
            _DOC_NAME in resp.headers.get("content-disposition", "")
        ), "a document keeps its original filename in Content-Disposition"

    async def test_message_without_key_returns_404(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        local_storage: Path,
    ) -> None:
        # Arrange: a message whose attachment has not been fetched yet (key NULL).
        conv_id, msg_id, _key = await _seed_attachment_message(
            db_session, tg_chat_id=_CHAT_A, with_key=False
        )

        # Act / Assert: no stored key → 404.
        resp = await client.get(f"{_BASE}/{conv_id}/attachments/{msg_id}")
        assert resp.status_code == 404, resp.text

    async def test_message_in_other_conversation_returns_404(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        local_storage: Path,
    ) -> None:
        # Arrange: a message stored under conversation A, plus a bare conversation B.
        _conv_a, msg_id, _key = await _seed_attachment_message(
            db_session, tg_chat_id=_CHAT_A
        )
        repo = SupportRepository(db_session)
        conv_b = await repo.create_conversation(
            tg_chat_id=_CHAT_B,
            customer_name="Ben",
            customer_username="ben",
            lang="ro",
        )
        await db_session.flush()

        # Act / Assert: the message id does not belong to conversation B → 404.
        resp = await client.get(f"{_BASE}/{conv_b.id}/attachments/{msg_id}")
        assert resp.status_code == 404, "a message from another conversation → 404"

    async def test_missing_storage_object_returns_404(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        local_storage: Path,
    ) -> None:
        # Arrange: a message with a key, but the object is removed from storage.
        conv_id, msg_id, key = await _seed_attachment_message(
            db_session, tg_chat_id=_CHAT_A
        )
        await get_storage().remove_key(key)

        # Act / Assert: the key resolves but the object is gone → 404.
        resp = await client.get(f"{_BASE}/{conv_id}/attachments/{msg_id}")
        assert resp.status_code == 404, "a missing storage object → 404"

    async def test_guest_guard_blocks_unauthenticated(
        self,
        guest_client: AsyncClient,
        db_session: AsyncSession,
        local_storage: Path,
    ) -> None:
        # Arrange: a fully-streamable attachment an unauthenticated caller wants.
        conv_id, msg_id, _key = await _seed_attachment_message(
            db_session, tg_chat_id=_CHAT_A
        )

        # Act / Assert: the current_staff guard blocks it (401/403).
        resp = await guest_client.get(f"{_BASE}/{conv_id}/attachments/{msg_id}")
        assert resp.status_code in (401, 403), resp.text


class ThreadAttachmentReadyTest:
    """``GET /conversations/{id}`` — MessageOut kind + attachment_ready flag."""

    async def test_ready_flips_false_to_true_when_key_is_set(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        local_storage: Path,
    ) -> None:
        # Arrange: a photo message NOT yet fetched (no stored key).
        conv_id, msg_id, _key = await _seed_attachment_message(
            db_session, tg_chat_id=_CHAT_A, with_key=False
        )

        # Act: read the thread before the key is set.
        resp_before = await client.get(f"{_BASE}/{conv_id}")

        # Assert: the message reports its kind but is not ready yet.
        assert resp_before.status_code == 200, resp_before.text
        msg_out = next(
            m for m in resp_before.json()["data"] if m["id"] == msg_id
        )
        assert msg_out["attachment_kind"] == "photo", "the kind is serialized"
        assert (
            msg_out["attachment_ready"] is False
        ), "a not-yet-fetched attachment is not ready"

        # Arrange: the fetch task records the stored object key.
        key = support_attachment_key(conv_id, ".jpg")
        await get_storage().put_key(
            key, _OBJECT_BYTES, content_type=_OBJECT_CONTENT_TYPE
        )
        msg = await SupportRepository(db_session).get_message(msg_id)
        msg.attachment_key = key
        await db_session.flush()

        # Act: read the thread again.
        resp_after = await client.get(f"{_BASE}/{conv_id}")

        # Assert: the attachment is now ready (the derived flag is True).
        assert resp_after.status_code == 200, resp_after.text
        msg_out = next(
            m for m in resp_after.json()["data"] if m["id"] == msg_id
        )
        assert (
            msg_out["attachment_ready"] is True
        ), "once the key is set the attachment is ready"
