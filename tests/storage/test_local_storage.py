"""Unit tests for the local filesystem storage backend (W7).

Verifies ``LocalStorage.save`` writes the bytes under ``media_root`` and returns
the public URL derived from ``media_url_prefix`` + ``/<name>``.
"""

from pathlib import Path

import pytest

from app.core.config import settings
from app.core.storage import LocalStorage, get_storage

pytestmark = pytest.mark.asyncio


async def test_local_save_writes_file_and_returns_url(tmp_path: Path) -> None:
    """``save`` persists the bytes and returns ``media_url_prefix/<name>``."""
    config = settings.model_copy(
        update={
            "storage_backend": "local",
            "media_root": str(tmp_path),
            "media_url_prefix": "/media",
        }
    )
    storage = LocalStorage(config)
    payload = b"local-bytes-\x00\x01\x02"

    url = await storage.save(payload, filename="pic.png", content_type="image/png")

    assert url.startswith("/media/")
    name = url.rsplit("/", 1)[-1]
    assert name.endswith(".png")
    written = tmp_path / name
    assert written.exists()
    assert written.read_bytes() == payload


async def test_get_storage_local_backend() -> None:
    """The factory returns a :class:`LocalStorage` for the ``local`` backend."""
    config = settings.model_copy(update={"storage_backend": "local"})
    assert isinstance(get_storage(config), LocalStorage)
