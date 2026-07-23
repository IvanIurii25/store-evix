"""Unit tests for :class:`S3Storage` with the aioboto3 client mocked.

These cover the S3 backend's ``save`` / ``save_variants`` / ``delete`` code paths
and the public-URL resolution **without** a running MinIO — the aioboto3 session
is replaced with an async-context-manager mock so the put/delete calls are
asserted directly. The real-MinIO integration tests live in
``test_s3_storage.py`` (skipped when the infra is down).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import settings
from app.core.storage import (
    _MEDIA_CACHE_CONTROL,
    S3Storage,
    get_storage,
)

pytestmark = pytest.mark.asyncio

_S3_ENDPOINT = "http://minio.test:9000"
_S3_BUCKET = "evix-media"


def _s3_config(**overrides):
    """Return a settings copy configured for the S3 backend."""
    base = {
        "storage_backend": "s3",
        "s3_endpoint_url": _S3_ENDPOINT,
        "s3_access_key": "key",
        "s3_secret_key": "secret",
        "s3_bucket": _S3_BUCKET,
        "s3_region": "us-east-1",
        "s3_public_url": "",
    }
    base.update(overrides)
    return settings.model_copy(update=base)


def _mock_session():
    """Return ``(session, client)`` where ``session.client(...)`` yields ``client``.

    ``client`` is an :class:`AsyncMock` (its ``put_object`` / ``delete_object``
    are awaitable); ``session.client(...)`` returns an async context manager
    entering that client, matching how :class:`S3Storage` uses aioboto3.
    """
    client = AsyncMock()
    context = AsyncMock()
    context.__aenter__.return_value = client
    context.__aexit__.return_value = False
    session = MagicMock()
    session.client = MagicMock(return_value=context)
    return session, client


def _storage_with_mock():
    """Build an :class:`S3Storage` whose aioboto3 session is mocked."""
    storage = S3Storage(_s3_config())
    session, client = _mock_session()
    storage._session = session
    return storage, client


async def test_save_puts_object_with_immutable_cache_and_returns_endpoint_url():
    """save() puts the object (immutable cache) and returns endpoint/bucket URL."""
    storage, client = _storage_with_mock()

    url = await storage.save(b"bytes", filename="banner.png", content_type="image/png")

    client.put_object.assert_awaited_once()
    kwargs = client.put_object.await_args.kwargs
    assert kwargs["Bucket"] == _S3_BUCKET
    assert kwargs["Key"].startswith("media/")
    assert kwargs["Key"].endswith(".png")
    assert kwargs["Body"] == b"bytes"
    assert kwargs["ContentType"] == "image/png"
    assert kwargs["CacheControl"] == _MEDIA_CACHE_CONTROL
    assert url == f"{_S3_ENDPOINT}/{_S3_BUCKET}/{kwargs['Key']}"


async def test_save_uses_public_url_base_when_configured():
    """When s3_public_url is set the returned URL uses that base + key."""
    storage = S3Storage(_s3_config(s3_public_url="https://cdn.test"))
    session, client = _mock_session()
    storage._session = session

    url = await storage.save(b"x", filename="p.jpg", content_type="image/jpeg")

    key = client.put_object.await_args.kwargs["Key"]
    assert url == f"https://cdn.test/{key}"


async def test_save_variants_puts_each_variant_as_webp():
    """save_variants() puts one immutable webp object per width."""
    storage, client = _storage_with_mock()

    await storage.save_variants(
        "https://cdn.test/media/abc.png", {400: b"w400", 800: b"w800"}
    )

    assert client.put_object.await_count == 2
    for call in client.put_object.await_args_list:
        assert call.kwargs["ContentType"] == "image/webp"
        assert call.kwargs["CacheControl"] == _MEDIA_CACHE_CONTROL
        assert call.kwargs["Key"].endswith(".webp")
    keys = {c.kwargs["Key"] for c in client.put_object.await_args_list}
    assert keys == {"media/abc_400.webp", "media/abc_800.webp"}


async def test_save_variants_empty_is_noop():
    """An empty variant mapping never opens an S3 client."""
    storage, client = _storage_with_mock()

    await storage.save_variants("https://cdn.test/media/abc.png", {})

    client.put_object.assert_not_awaited()


async def test_delete_removes_object_by_reconstructed_key():
    """delete() removes media/<basename> regardless of the public base."""
    storage, client = _storage_with_mock()

    await storage.delete("https://cdn.test/media/abc.png")

    client.delete_object.assert_awaited_once_with(
        Bucket=_S3_BUCKET, Key="media/abc.png"
    )


async def test_delete_empty_url_is_noop():
    """delete() with a base-less URL does not call S3."""
    storage, client = _storage_with_mock()

    await storage.delete("")

    client.delete_object.assert_not_awaited()


async def test_get_storage_unknown_backend_raises():
    """The factory rejects an unknown storage_backend with ValueError."""
    with pytest.raises(ValueError, match="Unknown storage_backend"):
        get_storage(_s3_config(storage_backend="ftp"))
