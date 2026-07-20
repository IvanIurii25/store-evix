"""Media storage abstraction with pluggable backends (W7).

Admin media uploads (§10) are persisted through a small storage interface so the
call site (:class:`~app.services.admin_catalog_service.AdminCatalogService`)
does not care *where* the bytes land. Two backends are provided, selected by
``settings.storage_backend``:

* ``local`` — writes to ``settings.media_root`` under a random ``uuid4`` name
  (original extension preserved) and returns
  ``settings.media_url_prefix`` + ``/<name>`` (the pre-W7 behaviour; the local
  static-mount serves it).
* ``s3`` — puts the object into ``settings.s3_bucket`` (MinIO/S3) via
  ``aioboto3`` under the key ``media/<uuid>.<ext>`` and returns a public URL:
  ``settings.s3_public_url`` + ``/<key>`` when the public base is configured,
  otherwise ``settings.s3_endpoint_url`` + ``/<bucket>/<key>``.

The single method ``save`` takes the raw bytes plus the original ``filename``
(only its extension is used) and the ``content_type``; it returns the public URL
the object is served from.
"""

import uuid
from abc import ABC, abstractmethod
from pathlib import Path

import aioboto3

from app.core.config import Settings, settings

# Object-key prefix for S3-stored media (keeps uploads grouped in the bucket).
_S3_KEY_PREFIX: str = "media"


def _object_name(filename: str) -> str:
    """Derive a random, collision-free object name preserving the extension.

    Args:
        filename: The original client filename (only its suffix is used).

    Returns:
        str: ``<uuid4hex><suffix>`` (suffix empty when the source has none).
    """
    suffix = Path(filename or "").suffix
    return f"{uuid.uuid4().hex}{suffix}"


class Storage(ABC):
    """Abstract media store: persist bytes, return a public URL."""

    @abstractmethod
    async def save(self, data: bytes, *, filename: str, content_type: str) -> str:
        """Persist ``data`` and return the public URL of the stored object.

        Args:
            data: The raw file contents to store.
            filename: The original filename (only its extension is used to name
                the stored object).
            content_type: The MIME type of the object (e.g. ``image/png``).

        Returns:
            str: The public URL the stored object is served from.
        """
        raise NotImplementedError

    @abstractmethod
    async def delete(self, url: str) -> None:
        """Best-effort removal of the object previously returned by ``save``.

        Implementations must not raise if the object is already gone — media
        deletion is driven by the DB row, and a missing/orphaned file must never
        block removing that row.

        Args:
            url: The public URL originally returned by :meth:`save`.
        """
        raise NotImplementedError


class LocalStorage(Storage):
    """Filesystem backend writing under ``media_root`` (dev / static-mount)."""

    def __init__(self, config: Settings) -> None:
        """Bind the backend to the media root + URL prefix from settings.

        Args:
            config: Application settings supplying ``media_root`` and
                ``media_url_prefix``.
        """
        self._root = Path(config.media_root)
        self._url_prefix = config.media_url_prefix.rstrip("/")

    async def save(self, data: bytes, *, filename: str, content_type: str) -> str:
        """Write ``data`` to ``media_root`` under a uuid name; return its URL.

        Args:
            data: The raw file contents to store.
            filename: The original filename (its extension is preserved).
            content_type: The MIME type (unused by the local backend).

        Returns:
            str: ``media_url_prefix`` + ``/<name>``.
        """
        name = _object_name(filename)
        self._root.mkdir(parents=True, exist_ok=True)
        destination = self._root / name
        with destination.open("wb") as out:
            out.write(data)
        return f"{self._url_prefix}/{name}"

    async def delete(self, url: str) -> None:
        """Unlink the file under ``media_root`` named by the URL's basename.

        Args:
            url: The public URL returned by :meth:`save`.
        """
        name = Path(url).name
        if name:
            (self._root / name).unlink(missing_ok=True)


class S3Storage(Storage):
    """Object-storage backend (MinIO/S3) using ``aioboto3.put_object``."""

    def __init__(self, config: Settings) -> None:
        """Bind the backend to the S3 connection + bucket from settings.

        Args:
            config: Application settings supplying the S3 endpoint, credentials,
                bucket, region and (optional) public base URL.
        """
        self._endpoint_url = config.s3_endpoint_url.rstrip("/")
        self._access_key = config.s3_access_key
        self._secret_key = config.s3_secret_key
        self._bucket = config.s3_bucket
        self._region = config.s3_region
        self._public_url = config.s3_public_url.rstrip("/")
        self._session = aioboto3.Session()

    def _public_url_for(self, key: str) -> str:
        """Build the public URL for a stored object key.

        Args:
            key: The object key inside the bucket.

        Returns:
            str: ``s3_public_url`` + ``/<key>`` when the public base is set,
            otherwise ``s3_endpoint_url`` + ``/<bucket>/<key>``.
        """
        if self._public_url:
            return f"{self._public_url}/{key}"
        return f"{self._endpoint_url}/{self._bucket}/{key}"

    async def save(self, data: bytes, *, filename: str, content_type: str) -> str:
        """Put ``data`` into the bucket under ``media/<uuid>.<ext>``; return URL.

        Args:
            data: The raw file contents to store.
            filename: The original filename (its extension is preserved).
            content_type: The MIME type stored with the object.

        Returns:
            str: The public URL of the stored object.
        """
        key = f"{_S3_KEY_PREFIX}/{_object_name(filename)}"
        async with self._session.client(
            "s3",
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name=self._region,
        ) as client:
            await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        return self._public_url_for(key)

    async def delete(self, url: str) -> None:
        """Delete the bucket object whose key is ``media/<basename-of-url>``.

        The key is reconstructed from the URL basename (``save`` always stores
        under ``media/<uuid>.<ext>``), so it is independent of whether a public
        CDN base is configured. Missing keys are a no-op on S3/MinIO.

        Args:
            url: The public URL returned by :meth:`save`.
        """
        name = Path(url).name
        if not name:
            return
        key = f"{_S3_KEY_PREFIX}/{name}"
        async with self._session.client(
            "s3",
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name=self._region,
        ) as client:
            await client.delete_object(Bucket=self._bucket, Key=key)


def get_storage(config: Settings | None = None) -> Storage:
    """Return the storage backend selected by ``settings.storage_backend``.

    Args:
        config: Optional settings override (defaults to the module ``settings``);
            useful for tests that flip ``storage_backend`` at runtime.

    Returns:
        Storage: A :class:`LocalStorage` for ``local`` or :class:`S3Storage`
        for ``s3``.

    Raises:
        ValueError: If ``storage_backend`` is not ``local`` or ``s3``.
    """
    config = config or settings
    backend = config.storage_backend
    if backend == "local":
        return LocalStorage(config)
    if backend == "s3":
        return S3Storage(config)
    raise ValueError(f"Unknown storage_backend: {backend!r}")
