"""Decode HTML entities in product NAMES (flystore import artifact).

The WooCommerce/flystore import stored product names with raw HTML entities —
``&#8212;`` (—), ``&#171;`` («), ``&#8217;`` (’) etc. They render as literal
gibberish in the storefront and hurt search (a query for "rubik's" misses
"rubik&#8217;s"). This one-shot decodes them with :func:`html.unescape`.

Scope, deliberately narrow:

* ONLY ``product_translation.name`` (both langs). Product **descriptions** are
  intentional full HTML (``<p><img …>``) and are left untouched — unescaping
  them would corrupt markup/URLs. SEO/category/attribute fields have no entities.
* Idempotent: re-running is a no-op (unescape of clean text == clean text). The
  survey found no double-encoded values, so a single pass suffices.

After decoding it rebuilds the affected ``product_card`` rows (denormalized name)
and re-indexes those products in Elasticsearch, so search + listings stay in sync.

DRY-RUN by default. Set ``CONFIRM=1`` to write. A backup of the changed rows
(product_id, lang, old/new name) is printed and written to ``ENTITY_BACKUP``
(default ``/tmp/name_entity_backup.json``) before any write.

Usage (in the app container):
    docker exec -i evix-store-app-1 uv run python scripts/clean_html_entities.py          # dry run
    docker exec -i evix-store-app-1 env CONFIRM=1 uv run python scripts/clean_html_entities.py
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models.catalog import ProductTranslation
from app.search.es import indexer
from app.search.es.client import new_es_client
from app.services.catalog_service import CatalogService

_ENTITY = re.compile(r"&#?[a-zA-Z0-9]+;")
CONFIRM = os.environ.get("CONFIRM") == "1"
BACKUP_PATH = os.environ.get("ENTITY_BACKUP", "/tmp/name_entity_backup.json")


async def main() -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with maker() as session:
            rows = (
                await session.execute(
                    select(ProductTranslation).where(
                        ProductTranslation.name.like("%&%;%")
                    )
                )
            ).scalars().all()

            changes = []
            for tr in rows:
                decoded = html.unescape(tr.name)
                # Guard against runaway double-decoding (none expected).
                twice = html.unescape(decoded)
                if twice != decoded:
                    decoded = twice
                if decoded != tr.name and not _ENTITY.search(decoded):
                    changes.append((tr, decoded))

            print(f"product_translation rows with entities: {len(rows)}")
            print(f"rows that will change:                  {len(changes)}")
            for tr, decoded in changes:
                print(f"  [{tr.product_id}/{tr.lang}] {tr.name!r} -> {decoded!r}")

            if not changes:
                print("nothing to do.")
                return

            backup = [
                {"product_id": tr.product_id, "lang": tr.lang,
                 "old": tr.name, "new": decoded}
                for tr, decoded in changes
            ]
            with open(BACKUP_PATH, "w", encoding="utf-8") as fh:
                json.dump(backup, fh, ensure_ascii=False, indent=2)
            print(f"backup written: {BACKUP_PATH}")

            if not CONFIRM:
                print("\nDRY RUN — set CONFIRM=1 to apply.")
                return

            product_ids = sorted({tr.product_id for tr, _ in changes})
            for tr, decoded in changes:
                tr.name = decoded
            await session.commit()
            print(f"updated {len(changes)} translation rows.")

            # Refresh the denormalized card names for affected products. NOTE:
            # rebuild_card only flush()es — the commit here is what persists the
            # rebuilt cards (and is also what the storefront reads for the display
            # name, since search hydrates the card, not the ES doc).
            rebuilt = await CatalogService(session).rebuild_cards(product_ids)
            await session.commit()
            print(f"rebuilt {rebuilt} product_card rows for {len(product_ids)} products.")

            # Re-index those products in Elasticsearch.
            client = new_es_client()
            try:
                await indexer.ensure_index(client)
                indexed = await indexer.index_products(session, client, product_ids)
                await client.indices.refresh(index=settings.elastic_index)
            finally:
                await client.close()
            print(f"re-indexed {indexed} products in ES.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
