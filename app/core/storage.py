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

import mimetypes
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from uuid import uuid4

import aioboto3
from botocore.exceptions import ClientError

from app.core.config import Settings, settings

# Object-key prefix for S3-stored media (keeps uploads grouped in the bucket).
_S3_KEY_PREFIX: str = "media"

# Object-key prefix for support attachments (customer photos/documents sent to
# the Telegram bot). Unlike ``media``, these are never served by a public URL —
# they are streamed by a staff-only proxy — so they use key-addressed
# put/fetch/remove (:meth:`Storage.put_key` etc.) instead of :meth:`Storage.save`.
SUPPORT_KEY_PREFIX: str = "support"

# Cache header for private support attachments: mutable lifecycle (erasure /
# retention delete them), never cached by shared caches.
_SUPPORT_CACHE_CONTROL: str = "private, no-store"

# Fallback content type for local support attachments whose type can't be
# guessed from the key.
_DEFAULT_CONTENT_TYPE: str = "application/octet-stream"

# Long-lived immutable cache header for all stored media. Objects are named by a
# random uuid (originals) or ``<uuid>_<width>.webp`` (variants), so a given key's
# bytes never change — safe to cache for a year. Applied to originals in
# :meth:`S3Storage.save` and to variants (mirrors scripts/generate_media_variants.py).
_MEDIA_CACHE_CONTROL: str = "public, max-age=31536000, immutable"
_VARIANT_CONTENT_TYPE: str = "image/webp"
_VARIANT_CACHE_CONTROL: str = _MEDIA_CACHE_CONTROL


def _variant_object_name(original_url: str, width: int) -> str:
    """Build a variant object name from an original URL and a width.

    Mirrors the backfill naming: the original's basename ``<name>.<ext>`` has its
    extension stripped and the variant becomes ``<name>_<width>.webp``.

    Args:
        original_url: The public URL of the stored original (only its basename is
            used).
        width: The variant width in pixels.

    Returns:
        str: ``<name>_<width>.webp`` (no directory prefix).
    """
    stem = Path(original_url).stem
    return f"{stem}_{width}.webp"


def _object_name(filename: str) -> str:
    """Derive a random, collision-free object name preserving the extension.

    Args:
        filename: The original client filename (only its suffix is used).

    Returns:
        str: ``<uuid4hex><suffix>`` (suffix empty when the source has none).
    """
    suffix = Path(filename or "").suffix
    return f"{uuid.uuid4().hex}{suffix}"


def support_attachment_key(conversation_id: int, ext: str) -> str:
    """Build a key-addressed object key for a support attachment.

    Support attachments are grouped per conversation under the private
    :data:`SUPPORT_KEY_PREFIX` and named by a random uuid so their bytes are
    stable and unguessable. Stored/fetched/removed via the key-addressed methods
    (:meth:`Storage.put_key` / :meth:`Storage.fetch_key` /
    :meth:`Storage.remove_key`), never by public URL.

    Args:
        conversation_id: The owning :class:`SupportConversation` id.
        ext: The file extension including the leading dot (e.g. ``.jpg``), or an
            empty string when the source has none.

    Returns:
        str: ``support/<conversation_id>/<uuid4hex><ext>``.
    """
    return f"{SUPPORT_KEY_PREFIX}/{conversation_id}/{uuid4().hex}{ext}"


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
    async def save_variants(
        self, original_url: str, variants: dict[int, bytes]
    ) -> None:
        """Persist the responsive WebP variants alongside a stored original.

        Each variant is stored next to the original under the object name
        ``<name>_<width>.webp``, where ``<name>`` is the original URL's basename
        with its extension stripped (mirrors the backfill's naming). Safe to call
        once per upload; overwriting an existing variant is fine.

        Args:
            original_url: The public URL previously returned by :meth:`save`.
            variants: A ``{width: webp_bytes}`` mapping (e.g. from
                :func:`app.core.images.validate_and_build_variants`).
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

    @abstractmethod
    async def put_key(self, key: str, data: bytes, *, content_type: str) -> None:
        """Store ``data`` at exactly ``key`` (no auto-naming) with a content type.

        Unlike :meth:`save`, the caller owns the key (see
        :func:`support_attachment_key`) and the object is *not* served by a public
        URL — it is streamed by a staff-only proxy. Objects are private and have a
        mutable lifecycle (erasure / retention delete them), so no long-lived
        immutable cache header is applied.

        Args:
            key: The exact object key to store the bytes under.
            data: The raw file contents to store.
            content_type: The MIME type stored with the object.
        """
        raise NotImplementedError

    @abstractmethod
    async def fetch_key(self, key: str) -> tuple[bytes, str] | None:
        """Return ``(data, content_type)`` for the object at ``key``, or ``None``.

        Used by the staff proxy to stream a support attachment.

        Args:
            key: The exact object key to read.

        Returns:
            tuple[bytes, str] | None: The object's bytes and MIME type, or
            ``None`` when no object exists at ``key``.
        """
        raise NotImplementedError

    @abstractmethod
    async def remove_key(self, key: str) -> None:
        """Delete the object at ``key`` (idempotent — missing is a no-op).

        Drives erasure / retention deletion of support attachments; a missing
        object must never raise.

        Args:
            key: The exact object key to delete.
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

    async def save_variants(
        self, original_url: str, variants: dict[int, bytes]
    ) -> None:
        """Write each WebP variant next to the original under ``media_root``.

        Args:
            original_url: The public URL returned by :meth:`save`.
            variants: A ``{width: webp_bytes}`` mapping to persist.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        for width, payload in variants.items():
            name = _variant_object_name(original_url, width)
            with (self._root / name).open("wb") as out:
                out.write(payload)

    async def delete(self, url: str) -> None:
        """Unlink the file under ``media_root`` named by the URL's basename.

        Args:
            url: The public URL returned by :meth:`save`.
        """
        name = Path(url).name
        if name:
            (self._root / name).unlink(missing_ok=True)

    async def put_key(self, key: str, data: bytes, *, content_type: str) -> None:
        """Write ``data`` under ``media_root`` at ``key`` (parent dirs created).

        Args:
            key: The exact object key (relative path under ``media_root``).
            data: The raw file contents to store.
            content_type: The MIME type (unused by the local backend).
        """
        destination = self._root / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as out:
            out.write(data)

    async def fetch_key(self, key: str) -> tuple[bytes, str] | None:
        """Read the file at ``key`` under ``media_root`` and guess its type.

        Args:
            key: The exact object key (relative path under ``media_root``).

        Returns:
            tuple[bytes, str] | None: The file's bytes and a ``mimetypes``-guessed
            content type (fallback ``application/octet-stream``), or ``None`` when
            the file is missing.
        """
        source = self._root / key
        if not source.is_file():
            return None
        data = source.read_bytes()
        content_type = mimetypes.guess_type(key)[0] or _DEFAULT_CONTENT_TYPE
        return data, content_type

    async def remove_key(self, key: str) -> None:
        """Unlink the file at ``key`` under ``media_root`` (missing is a no-op).

        Args:
            key: The exact object key (relative path under ``media_root``).
        """
        (self._root / key).unlink(missing_ok=True)


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
                CacheControl=_MEDIA_CACHE_CONTROL,
            )
        return self._public_url_for(key)

    async def save_variants(
        self, original_url: str, variants: dict[int, bytes]
    ) -> None:
        """Put each WebP variant under ``media/<name>_<width>.webp`` in the bucket.

        Mirrors the backfill exactly: ``image/webp`` content type and a long-lived
        immutable ``Cache-Control`` on every variant object.

        Args:
            original_url: The public URL returned by :meth:`save`.
            variants: A ``{width: webp_bytes}`` mapping to persist.
        """
        if not variants:
            return
        async with self._session.client(
            "s3",
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name=self._region,
        ) as client:
            for width, payload in variants.items():
                key = f"{_S3_KEY_PREFIX}/{_variant_object_name(original_url, width)}"
                await client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=payload,
                    ContentType=_VARIANT_CONTENT_TYPE,
                    CacheControl=_VARIANT_CACHE_CONTROL,
                )

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

    async def put_key(self, key: str, data: bytes, *, content_type: str) -> None:
        """Put ``data`` into the bucket at exactly ``key`` (private, mutable).

        No long-lived immutable ``Cache-Control`` is set — support attachments are
        private and their lifecycle is mutable (erasure / retention delete them).

        Args:
            key: The exact object key to store the bytes under.
            data: The raw file contents to store.
            content_type: The MIME type stored with the object.
        """
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
                CacheControl=_SUPPORT_CACHE_CONTROL,
            )

    async def fetch_key(self, key: str) -> tuple[bytes, str] | None:
        """Get the bucket object at ``key``; return its bytes + content type.

        Args:
            key: The exact object key to read.

        Returns:
            tuple[bytes, str] | None: The object's bytes and stored ``ContentType``
            (fallback ``application/octet-stream``), or ``None`` when no object
            exists at ``key``.
        """
        async with self._session.client(
            "s3",
            endpoint_url=self._endpoint_url,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            region_name=self._region,
        ) as client:
            try:
                response = await client.get_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code in ("NoSuchKey", "404", "NoSuchBucket"):
                    return None
                raise
            async with response["Body"] as body:
                data = await body.read()
            content_type = response.get("ContentType") or _DEFAULT_CONTENT_TYPE
            return data, content_type

    async def remove_key(self, key: str) -> None:
        """Delete the bucket object at ``key`` (missing keys are a no-op on S3).

        Args:
            key: The exact object key to delete.
        """
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
