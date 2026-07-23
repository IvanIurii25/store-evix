"""Unit tests for the local filesystem storage backend (W7).

Verifies ``LocalStorage.save`` writes the bytes under ``media_root`` and returns
the public URL derived from ``media_url_prefix`` + ``/<name>``.
"""

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

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


async def test_local_save_variants_writes_webp_files(tmp_path: Path) -> None:
    """``save_variants`` writes each ``<name>_<W>.webp`` next to the original."""
    config = settings.model_copy(
        update={
            "storage_backend": "local",
            "media_root": str(tmp_path),
            "media_url_prefix": "/media",
        }
    )
    storage = LocalStorage(config)
    original_url = await storage.save(
        b"orig", filename="pic.png", content_type="image/png"
    )
    stem = original_url.rsplit("/", 1)[-1].rsplit(".", 1)[0]

    def _webp(width: int) -> bytes:
        buffer = BytesIO()
        Image.new("RGB", (width, width), "blue").save(buffer, format="WEBP")
        return buffer.getvalue()

    variants = {200: _webp(200), 400: _webp(400)}
    await storage.save_variants(original_url, variants)

    for width, payload in variants.items():
        written = tmp_path / f"{stem}_{width}.webp"
        assert written.exists()
        assert written.read_bytes() == payload
        assert Image.open(BytesIO(written.read_bytes())).format == "WEBP"


async def test_get_storage_local_backend() -> None:
    """The factory returns a :class:`LocalStorage` for the ``local`` backend."""
    config = settings.model_copy(update={"storage_backend": "local"})
    assert isinstance(get_storage(config), LocalStorage)
