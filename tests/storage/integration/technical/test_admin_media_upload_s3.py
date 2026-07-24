"""API test: admin media upload routed through the S3 backend (W7).

Flips ``settings.storage_backend`` to ``s3`` (pointed at the local MinIO) via
monkeypatch, then uploads an image through the admin endpoint and asserts a
:class:`Media` row is created with an S3 public URL and that the object is
actually downloadable from MinIO. Skips (does not fail) when MinIO is down.
"""

from io import BytesIO

import httpx
import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Media

pytestmark = pytest.mark.asyncio


def _png_bytes(width: int = 1000, height: int = 800) -> bytes:
    """Return an in-memory, genuinely decodable PNG (the upload gate needs one)."""
    buffer = BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format="PNG")
    return buffer.getvalue()


_S3_ENDPOINT: str = "http://localhost:59000"
_S3_ACCESS_KEY: str = "evixminio"
_S3_SECRET_KEY: str = "evixminio-secret"
_S3_BUCKET: str = "evix-media"


def _product_translations(slug: str) -> list[dict]:
    """Two-language product translations for a publishable product."""
    return [
        {"lang": lang, "name": f"phone-{lang}", "slug": f"{slug}-{lang}"}
        for lang in ("ru", "ro")
    ]


async def _make_category(client: AsyncClient) -> int:
    """Create a category and return its id."""
    response = await client.post(
        "/api/v1/admin/categories",
        json={
            "parent_id": None,
            "is_active": False,
            "translations": [
                {"lang": "ru", "name": "cat-ru", "slug": "cat-ru"},
                {"lang": "ro", "name": "cat-ro", "slug": "cat-ro"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _skip_if_minio_down() -> None:
    """Skip the current test when the MinIO health endpoint is unreachable."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as probe:
            await probe.get(f"{_S3_ENDPOINT}/minio/health/live")
    except httpx.HTTPError as exc:
        pytest.skip(f"MinIO unreachable at {_S3_ENDPOINT}: {exc}")


async def test_admin_upload_uses_s3_backend(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    """Upload with ``storage_backend='s3'`` stores an S3 object + Media row."""
    await _skip_if_minio_down()

    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "storage_backend", "s3")
    monkeypatch.setattr(cfg.settings, "s3_endpoint_url", _S3_ENDPOINT)
    monkeypatch.setattr(cfg.settings, "s3_access_key", _S3_ACCESS_KEY)
    monkeypatch.setattr(cfg.settings, "s3_secret_key", _S3_SECRET_KEY)
    monkeypatch.setattr(cfg.settings, "s3_bucket", _S3_BUCKET)
    monkeypatch.setattr(cfg.settings, "s3_public_url", "")

    category_id = await _make_category(client)
    product = await client.post(
        "/api/v1/admin/products",
        json={
            "category_id": category_id,
            "code": "SKU-S3",
            "price": "12.00",
            "translations": _product_translations("s3img"),
        },
    )
    assert product.status_code == 201, product.text
    product_id = product.json()["id"]

    source = _png_bytes()
    image = BytesIO(source)
    response = await client.post(
        f"/api/v1/admin/products/{product_id}/media",
        files={"file": ("photo.png", image, "image/png")},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    expected_prefix = f"{_S3_ENDPOINT}/{_S3_BUCKET}/media/"
    assert body["url"].startswith(expected_prefix), body["url"]
    assert body["url"].endswith(".png")
    assert body["kind"] == "image"

    # The Media row is persisted with the S3 url.
    media = (
        await db_session.execute(select(Media).where(Media.product_id == product_id))
    ).scalar_one()
    assert media.url == body["url"]

    # The original object is downloadable from MinIO by the stored public URL.
    async with httpx.AsyncClient(timeout=10.0) as downloader:
        obj = await downloader.get(body["url"])
        assert obj.status_code == 200, obj.text
        assert obj.content == source

        # A responsive WebP variant is stored alongside it.
        variant_url = body["url"].rsplit(".", 1)[0] + "_400.webp"
        variant = await downloader.get(variant_url)
    assert variant.status_code == 200, variant.text
    assert Image.open(BytesIO(variant.content)).format == "WEBP"
