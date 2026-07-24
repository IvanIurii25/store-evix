"""Backfill an immutable ``Cache-Control`` header onto existing media objects.

Media objects are content-addressed by a random uuid (originals) or
``<uuid>_<width>.webp`` (variants), so their bytes never change — they should be
served ``public, max-age=31536000, immutable``. New uploads get this at write
time (``S3Storage.save`` / ``save_variants``); this one-off fixes objects stored
before that. Idempotent: an object already carrying the header is skipped.

Run on the server after deploying the backend:

    docker compose -f docker-compose.prod.yml exec app \\
        uv run python scripts/set_media_cache_headers.py [--dry-run] [--limit N]
"""

import argparse
import asyncio
import logging

import aioboto3

from app.core.config import settings
from app.core.storage import _MEDIA_CACHE_CONTROL, _S3_KEY_PREFIX

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("set_media_cache_headers")


def _client(session: aioboto3.Session):
    """Build an S3 client bound to the configured MinIO/S3 endpoint."""
    return session.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
    )


async def main(dry_run: bool, limit: int | None) -> None:
    """Set the immutable Cache-Control on every media object that lacks it."""
    bucket = settings.s3_bucket
    scanned = updated = skipped = 0
    session = aioboto3.Session()
    async with _client(session) as client:
        paginator = client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(
            Bucket=bucket, Prefix=f"{_S3_KEY_PREFIX}/"
        ):
            for obj in page.get("Contents", []):
                if limit is not None and scanned >= limit:
                    logger.info("reached --limit %d", limit)
                    _summary(scanned, updated, skipped)
                    return
                key = obj["Key"]
                scanned += 1
                head = await client.head_object(Bucket=bucket, Key=key)
                if head.get("CacheControl") == _MEDIA_CACHE_CONTROL:
                    skipped += 1
                    continue
                if dry_run:
                    logger.info("would set cache-control on %s", key)
                    updated += 1
                    continue
                # copy_object onto itself, replacing metadata (preserving type).
                await client.copy_object(
                    Bucket=bucket,
                    Key=key,
                    CopySource={"Bucket": bucket, "Key": key},
                    MetadataDirective="REPLACE",
                    ContentType=head.get("ContentType", "application/octet-stream"),
                    CacheControl=_MEDIA_CACHE_CONTROL,
                )
                updated += 1
    _summary(scanned, updated, skipped)


def _summary(scanned: int, updated: int, skipped: int) -> None:
    """Log the run totals."""
    logger.info("done: scanned=%d updated=%d skipped=%d", scanned, updated, skipped)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="log without writing")
    parser.add_argument(
        "--limit", type=int, default=None, help="process first N objects"
    )
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, limit=args.limit))
