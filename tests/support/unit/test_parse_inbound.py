"""Unit tests for ``parse_inbound`` (pure, no DB, no network).

``parse_inbound`` defensively extracts a private text message from a raw
Telegram update dict, mapping every field onto :class:`InboundMessage`, and
returns ``None`` (never raises) for every shape this MVP does not handle.
"""

import pytest

from app.core.telegram import InboundMessage, parse_inbound

# Telegram identifiers used in the valid-update fixtures.
_CHAT_ID: int = 555
_MESSAGE_ID: int = 10


def _valid_update() -> dict:
    """Return a realistic private text-message Telegram update dict."""
    return {
        "update_id": 1,
        "message": {
            "message_id": _MESSAGE_ID,
            "chat": {"id": _CHAT_ID},
            "from": {
                "id": _CHAT_ID,
                "first_name": "Ana",
                "username": "ana",
                "language_code": "ro",
            },
            "text": "Salut",
        },
    }


def test_valid_text_message_parsed() -> None:
    """A realistic private text update maps every field onto InboundMessage."""
    # Arrange
    update = _valid_update()

    # Act
    result = parse_inbound(update)

    # Assert
    assert isinstance(result, InboundMessage), "a text message must parse"
    assert result.chat_id == _CHAT_ID, "chat.id must map to chat_id"
    assert result.message_id == _MESSAGE_ID, "message_id must map through"
    assert result.text == "Salut", "text must map through"
    assert result.customer_name == "Ana", "first_name becomes the name"
    assert result.customer_username == "ana", "username maps through"
    assert result.lang == "ro", "language_code maps to lang"


def test_missing_message_returns_none() -> None:
    """An update with no ``message`` key (e.g. a poll update) is ignored."""
    # Arrange / Act / Assert
    assert parse_inbound({"update_id": 1}) is None, "no message → None"


def test_edited_message_only_returns_none() -> None:
    """An ``edited_message``-only update is ignored (MVP handles fresh only)."""
    # Arrange
    update = {"update_id": 1, "edited_message": {"text": "fixed", "chat": {}}}

    # Act / Assert
    assert parse_inbound(update) is None, "edited_message-only → None"


def test_non_dict_message_returns_none() -> None:
    """A ``message`` that is not a dict is guarded (returns None, no raise)."""
    # Arrange / Act / Assert
    assert parse_inbound({"message": "not-a-dict"}) is None, "non-dict message → None"


@pytest.mark.parametrize(
    "text_value",
    [None, ""],
    ids=["missing_text", "empty_text"],
)
def test_missing_or_empty_text_returns_none(text_value: str | None) -> None:
    """A message with absent/empty text (photo/sticker) is ignored."""
    # Arrange
    message: dict = {"message_id": 1, "chat": {"id": 1}}
    if text_value is not None:
        message["text"] = text_value
    update = {"message": message}

    # Act / Assert
    assert parse_inbound(update) is None, "missing/empty text → None"


def test_missing_chat_id_returns_none() -> None:
    """A text message with no ``chat.id`` cannot be routed → ignored."""
    # Arrange
    update = {"message": {"message_id": 1, "text": "hi", "chat": {}}}

    # Act / Assert
    assert parse_inbound(update) is None, "missing chat.id → None"


def test_missing_message_id_returns_none() -> None:
    """A text message with no ``message_id`` cannot be deduped → ignored."""
    # Arrange
    update = {"message": {"chat": {"id": 1}, "text": "hi"}}

    # Act / Assert
    assert parse_inbound(update) is None, "missing message_id → None"


def test_name_joins_first_and_last() -> None:
    """A sender with both names yields the space-joined ``first last``."""
    # Arrange
    update = _valid_update()
    update["message"]["from"] = {"first_name": "Ana", "last_name": "Popescu"}

    # Act
    result = parse_inbound(update)

    # Assert
    assert result is not None
    assert result.customer_name == "Ana Popescu", "name joins first + last"


def test_name_first_only() -> None:
    """A sender with only a first name yields it alone (no trailing space)."""
    # Arrange
    update = _valid_update()
    update["message"]["from"] = {"first_name": "Ana"}

    # Act
    result = parse_inbound(update)

    # Assert
    assert result is not None
    assert result.customer_name == "Ana", "name is first_name alone"


def test_name_neither_is_none() -> None:
    """A sender with no name parts collapses the empty join to ``None``."""
    # Arrange
    update = _valid_update()
    update["message"]["from"] = {"username": "ana"}

    # Act
    result = parse_inbound(update)

    # Assert
    assert result is not None
    assert result.customer_name is None, "no name parts → None"


def test_missing_from_yields_no_name() -> None:
    """A text message with no ``from`` still parses (name/username/lang None)."""
    # Arrange
    update = {"message": {"message_id": 1, "chat": {"id": 1}, "text": "hi"}}

    # Act
    result = parse_inbound(update)

    # Assert
    assert result is not None, "missing 'from' must still parse the text"
    assert result.customer_name is None, "no sender → no name"
    assert result.customer_username is None, "no sender → no username"
    assert result.lang is None, "no sender → no lang"
