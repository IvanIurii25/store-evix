"""Integration tests for the S3 storage backend against MinIO (W7).

Requires a reachable MinIO at ``localhost:59000`` with the ``evix-media`` bucket
(public-read). ``save`` must put the object and the returned public URL must be
downloadable. If MinIO is unreachable the test is skipped with an explicit
message rather than failing.
"""

import httpx
import pytest

from app.core.config import settings
from app.core.storage import S3Storage, get_storage

pytestmark = pytest.mark.asyncio

# MinIO connection for the storage integration tests (matches the W7 spec).
_S3_ENDPOINT: str = "http://localhost:59000"
_S3_ACCESS_KEY: str = "evixminio"
_S3_SECRET_KEY: str = "evixminio-secret"
_S3_BUCKET: str = "evix-media"
_S3_REGION: str = "us-east-1"


def _s3_config():
    """Return a settings copy pointing the S3 backend at the local MinIO."""
    return settings.model_copy(
        update={
            "storage_backend": "s3",
            "s3_endpoint_url": _S3_ENDPOINT,
            "s3_access_key": _S3_ACCESS_KEY,
            "s3_secret_key": _S3_SECRET_KEY,
            "s3_bucket": _S3_BUCKET,
            "s3_region": _S3_REGION,
            "s3_public_url": "",
        }
    )


async def _skip_if_minio_down() -> None:
    """Skip the current test when the MinIO health endpoint is unreachable."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.get(f"{_S3_ENDPOINT}/minio/health/live")
    except httpx.HTTPError as exc:
        pytest.skip(f"MinIO unreachable at {_S3_ENDPOINT}: {exc}")


async def test_get_storage_s3_backend() -> None:
    """The factory returns an :class:`S3Storage` for the ``s3`` backend."""
    assert isinstance(get_storage(_s3_config()), S3Storage)


async def test_s3_save_puts_object_downloadable_by_url() -> None:
    """``save`` uploads the object; the returned public URL serves the bytes."""
    await _skip_if_minio_down()

    storage = S3Storage(_s3_config())
    payload = b"s3-integration-bytes-\x10\x20\x30"

    url = await storage.save(payload, filename="banner.png", content_type="image/png")

    expected_prefix = f"{_S3_ENDPOINT}/{_S3_BUCKET}/media/"
    assert url.startswith(expected_prefix), url
    assert url.endswith(".png")

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url)
    assert response.status_code == 200, response.text
    assert response.content == payload


async def test_s3_save_uses_public_url_when_set() -> None:
    """When ``s3_public_url`` is set the returned URL uses that base + key."""
    await _skip_if_minio_down()

    public_base = f"{_S3_ENDPOINT}/{_S3_BUCKET}"
    config = _s3_config().model_copy(update={"s3_public_url": public_base})
    storage = S3Storage(config)

    url = await storage.save(
        b"public-url-bytes", filename="x.jpg", content_type="image/jpeg"
    )

    assert url.startswith(f"{public_base}/media/"), url
    assert url.endswith(".jpg")

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url)
    assert response.status_code == 200, response.text
    assert response.content == b"public-url-bytes"
