"""Unit tests for ``parse_inbound`` attachment handling (pure, no DB, no network).

``parse_inbound`` now also recognizes a photo or document message: it maps the
attachment reference (``attachment_file_id`` / ``attachment_kind`` /
``attachment_name``) onto :class:`InboundMessage`, uses the caption (or ``""``)
as ``text``, and still returns ``None`` for a shape carrying neither text nor a
recognized attachment (e.g. a sticker). A plain text message keeps its previous
shape (all attachment fields ``None``).
"""

from app.core.telegram import InboundMessage, parse_inbound

# Telegram identifiers used in the attachment-update fixtures.
_CHAT_ID: int = 555
_MESSAGE_ID: int = 10
# Telegram file ids for the two photo sizes (only the largest/last is kept).
_SMALL_FILE_ID: str = "small-size-file-id"
_LARGE_FILE_ID: str = "large-size-file-id"
# Telegram file id + original filename for a document attachment.
_DOC_FILE_ID: str = "doc-file-id"
_DOC_NAME: str = "invoice.pdf"
# A caption riding along with an attachment.
_CAPTION: str = "смотрите вложение"


def _base_message() -> dict:
    """Return the common ``message`` envelope (chat + sender) sans payload."""
    return {
        "message_id": _MESSAGE_ID,
        "chat": {"id": _CHAT_ID},
        "from": {
            "id": _CHAT_ID,
            "first_name": "Ana",
            "username": "ana",
            "language_code": "ro",
        },
    }


def test_photo_update_keeps_last_size_file_id_and_empty_text() -> None:
    """A photo (list of sizes, no caption) → photo kind, last size id, text ""."""
    # Arrange: a photo message with two sizes and no caption.
    message = _base_message()
    message["photo"] = [
        {"file_id": _SMALL_FILE_ID},
        {"file_id": _LARGE_FILE_ID},
    ]
    update = {"message": message}

    # Act
    result = parse_inbound(update)

    # Assert: the largest (last) size's file id is kept, name is None, text "".
    assert isinstance(result, InboundMessage), "a photo message must parse"
    assert result.attachment_kind == "photo", "photo list → photo kind"
    assert (
        result.attachment_file_id == _LARGE_FILE_ID
    ), "the last (largest) size's file_id is kept"
    assert result.attachment_name is None, "a photo carries no filename"
    assert result.text == "", "no caption → empty text"


def test_photo_update_with_caption_uses_caption_as_text() -> None:
    """A photo with a caption maps the caption onto ``text``."""
    # Arrange: a photo message carrying a caption.
    message = _base_message()
    message["photo"] = [{"file_id": _LARGE_FILE_ID}]
    message["caption"] = _CAPTION
    update = {"message": message}

    # Act
    result = parse_inbound(update)

    # Assert: the caption becomes the text.
    assert result is not None
    assert result.attachment_kind == "photo", "photo list → photo kind"
    assert result.text == _CAPTION, "the caption becomes the message text"


def test_document_update_keeps_file_id_and_filename() -> None:
    """A document → document kind, its file id + file_name, caption/"" as text."""
    # Arrange: a document message with no caption.
    message = _base_message()
    message["document"] = {"file_id": _DOC_FILE_ID, "file_name": _DOC_NAME}
    update = {"message": message}

    # Act
    result = parse_inbound(update)

    # Assert: the document's file id + original filename map through; text "".
    assert result is not None
    assert result.attachment_kind == "document", "document dict → document kind"
    assert result.attachment_file_id == _DOC_FILE_ID, "document file_id maps through"
    assert result.attachment_name == _DOC_NAME, "document file_name maps through"
    assert result.text == "", "no caption → empty text"


def test_sticker_only_message_returns_none() -> None:
    """A message with neither text nor a photo/document (sticker) is ignored."""
    # Arrange: a sticker message — no text, no photo, no document.
    message = _base_message()
    message["sticker"] = {"file_id": "sticker-file-id"}
    update = {"message": message}

    # Act / Assert
    assert parse_inbound(update) is None, "neither text nor attachment → None"


def test_photo_size_without_file_id_is_ignored() -> None:
    """A photo size list whose entry carries no ``file_id`` is not an attachment."""
    # Arrange: a photo list with a malformed (no file_id) size, no text.
    message = _base_message()
    message["photo"] = [{"width": 90, "height": 90}]
    update = {"message": message}

    # Act / Assert: no usable file id → falls through to the no-text guard → None.
    assert parse_inbound(update) is None, "a photo size without file_id → None"


def test_document_without_file_id_is_ignored() -> None:
    """A document dict carrying no ``file_id`` is not treated as an attachment."""
    # Arrange: a document with only a filename (no file_id), no text.
    message = _base_message()
    message["document"] = {"file_name": _DOC_NAME}
    update = {"message": message}

    # Act / Assert: no usable file id → falls through → None.
    assert parse_inbound(update) is None, "a document without file_id → None"


def test_plain_text_message_has_no_attachment_fields() -> None:
    """A plain text message still parses with all attachment fields ``None``."""
    # Arrange: a normal text message.
    message = _base_message()
    message["text"] = "Salut"
    update = {"message": message}

    # Act
    result = parse_inbound(update)

    # Assert: the text maps through and no attachment metadata is attached.
    assert result is not None
    assert result.text == "Salut", "text maps through"
    assert result.attachment_file_id is None, "a text message has no file id"
    assert result.attachment_kind is None, "a text message has no attachment kind"
    assert result.attachment_name is None, "a text message has no attachment name"
