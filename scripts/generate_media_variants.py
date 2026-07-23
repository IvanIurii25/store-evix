"""Backfill responsive WebP variants for existing MinIO/S3 media originals.

For every *original* raster object under the ``media/`` prefix in the configured
bucket (``media/<name>.<jpg|jpeg|png>``), this generates a fixed set of WebP
variants — ``media/<name>_<W>.webp`` for each ``W`` in :data:`WIDTHS` — using
:func:`app.core.images.generate_fixed_webp_set`, which returns an entry for
*every* requested width (capping to the source width, never up-scaling) so the
front's ``srcset`` URLs never 404.

Originals vs variants: a key ``media/<name>.<ext>`` is an *original* when ``ext``
is one of jpg/jpeg/png (case-insensitive) **and** ``<name>`` does not already end
with a ``_<digits>`` suffix. Anything ``.webp`` (or ``<name>_<digits>.<ext>``) is
treated as a generated variant and skipped as a source.

Idempotent: an original whose four variant keys already exist is skipped (unless
``--force``). Variants are written with a long-lived immutable ``Cache-Control``.

All S3 settings come from :mod:`app.core.config` (env-driven) — nothing is
hardcoded — so this runs unchanged in the prod app container::

    docker compose exec app uv run python scripts/generate_media_variants.py \
        --dry-run --limit 10

Usage:
    uv run python scripts/generate_media_variants.py [--dry-run] [--limit N] \
        [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import PurePosixPath

import aioboto3
from botocore.exceptions import ClientError

from app.core.config import settings
from app.core.images import RESPONSIVE_WIDTHS, generate_fixed_webp_set

logger = logging.getLogger("generate_media_variants")

# Fixed responsive width set — shared with the upload path (single source of
# truth); one WebP variant per width, per original.
WIDTHS: tuple[int, ...] = RESPONSIVE_WIDTHS

# Object-key prefix under which all media lives in the bucket.
_MEDIA_PREFIX: str = "media/"

# Raster extensions we treat as backfillable originals (case-insensitive).
_ORIGINAL_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png"})

# Cache header for the immutable, content-addressed WebP variants (1 year).
_VARIANT_CACHE_CONTROL: str = "public, max-age=31536000, immutable"

# A trailing ``_<digits>`` stem marks an already-generated variant, not a source.
_VARIANT_SUFFIX_RE: re.Pattern[str] = re.compile(r"_\d+$")


class _Summary:
    """Mutable run tally, rendered once at the end."""

    def __init__(self) -> None:
        """Initialise all counters to zero."""
        self.scanned = 0
        self.processed = 0
        self.written = 0
        self.skipped = 0

    def render(self) -> str:
        """Return the final one-line summary of the run.

        Returns:
            str: A human-readable summary of the four counters.
        """
        return (
            f"done: originals_scanned={self.scanned} "
            f"originals_processed={self.processed} "
            f"variants_written={self.written} "
            f"originals_skipped={self.skipped}"
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line flags for the backfill run.

    Args:
        argv: Optional explicit argument list (defaults to ``sys.argv``).

    Returns:
        argparse.Namespace: Parsed ``dry_run``, ``limit`` and ``force`` flags.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List variants that WOULD be generated per original; upload nothing.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N originals (default: all).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate variants even when they already exist.",
    )
    return parser.parse_args(argv)


def _is_original_key(key: str) -> bool:
    """Return whether ``key`` names an original raster (not a generated variant).

    Args:
        key: The full S3 object key (e.g. ``media/abc.jpg``).

    Returns:
        bool: ``True`` for ``media/<name>.<jpg|jpeg|png>`` whose ``<name>`` does
        not end with a ``_<digits>`` variant suffix; ``False`` otherwise
        (including any ``.webp`` object).
    """
    path = PurePosixPath(key)
    if path.suffix.lower() not in _ORIGINAL_EXTENSIONS:
        return False
    return _VARIANT_SUFFIX_RE.search(path.stem) is None


def _variant_key(base: str, width: int) -> str:
    """Build the variant key for an original base and width.

    Args:
        base: The original key with its extension stripped (``media/<name>``).
        width: The target width in pixels.

    Returns:
        str: ``media/<name>_<width>.webp``.
    """
    return f"{base}_{width}.webp"


async def _list_originals(client) -> list[str]:  # noqa: ANN001
    """Return every original raster key under the media prefix, paginated.

    Args:
        client: An open aioboto3 S3 client.

    Returns:
        list[str]: The original keys, in listing order.
    """
    keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    async for page in paginator.paginate(
        Bucket=settings.s3_bucket, Prefix=_MEDIA_PREFIX
    ):
        for entry in page.get("Contents", []):
            key = entry["Key"]
            if _is_original_key(key):
                keys.append(key)
    return keys


async def _key_exists(client, key: str) -> bool:  # noqa: ANN001
    """Return whether ``key`` exists in the bucket via ``head_object``.

    Args:
        client: An open aioboto3 S3 client.
        key: The object key to probe.

    Returns:
        bool: ``True`` if the object exists, ``False`` on a 404.

    Raises:
        ClientError: For any non-404 S3 error.
    """
    try:
        await client.head_object(Bucket=settings.s3_bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}:
            return False
        raise


async def _all_variants_exist(client, base: str) -> bool:  # noqa: ANN001
    """Return whether all variant keys for ``base`` already exist.

    Args:
        client: An open aioboto3 S3 client.
        base: The original key with its extension stripped (``media/<name>``).

    Returns:
        bool: ``True`` only if every ``WIDTHS`` variant key is present.
    """
    for width in WIDTHS:
        if not await _key_exists(client, _variant_key(base, width)):
            return False
    return True


async def _process_original(
    client,  # noqa: ANN001
    key: str,
    *,
    dry_run: bool,
    force: bool,
    summary: _Summary,
) -> None:
    """Generate and upload the WebP variant set for a single original.

    Args:
        client: An open aioboto3 S3 client.
        key: The original object key (``media/<name>.<ext>``).
        dry_run: When ``True``, log the planned variants but upload nothing.
        force: When ``True``, regenerate even if all variants already exist.
        summary: The run tally to update in place.
    """
    base = str(PurePosixPath(key).with_suffix(""))

    if not force and await _all_variants_exist(client, base):
        summary.skipped += 1
        logger.info("skip (complete): %s", key)
        return

    if dry_run:
        planned = [_variant_key(base, width) for width in WIDTHS]
        summary.processed += 1
        logger.info("would generate for %s -> %s", key, ", ".join(planned))
        return

    response = await client.get_object(Bucket=settings.s3_bucket, Key=key)
    async with response["Body"] as body:
        data = await body.read()

    variants = await asyncio.to_thread(generate_fixed_webp_set, data, WIDTHS)

    for width in WIDTHS:
        await client.put_object(
            Bucket=settings.s3_bucket,
            Key=_variant_key(base, width),
            Body=variants[width],
            ContentType="image/webp",
            CacheControl=_VARIANT_CACHE_CONTROL,
        )
        summary.written += 1

    summary.processed += 1
    logger.info("generated %d variants for %s", len(WIDTHS), key)


async def main(argv: list[str] | None = None) -> int:
    """Run the media-variant backfill and print a summary.

    Args:
        argv: Optional explicit argument list (defaults to ``sys.argv``).

    Returns:
        int: Process exit code (``0`` on success).
    """
    args = _parse_args(argv)
    session = aioboto3.Session()
    summary = _Summary()

    async with session.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url.rstrip("/"),
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
    ) as client:
        originals = await _list_originals(client)
        if args.limit is not None:
            originals = originals[: args.limit]
        summary.scanned = len(originals)
        logger.info(
            "scanning %d original(s) under %s (dry_run=%s, force=%s)",
            summary.scanned,
            _MEDIA_PREFIX,
            args.dry_run,
            args.force,
        )

        for key in originals:
            await _process_original(
                client,
                key,
                dry_run=args.dry_run,
                force=args.force,
                summary=summary,
            )

    logger.info(summary.render())
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    sys.exit(asyncio.run(main()))
