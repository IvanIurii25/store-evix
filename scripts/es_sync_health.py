"""Check that Elasticsearch is in sync with Postgres (the source of truth).

Compares product counts and id sets between the ``product`` table and the
``evix_products`` ES index and prints a verdict. Read-only — safe to run anytime.

Usage (in the app container, which can reach ``elasticsearch:9200``):
    docker exec -i evix-store-app-1 uv run python scripts/es_sync_health.py

Drift here means the incremental write-hook missed something; the nightly
``search.reindex_all`` reconciles, or run it now:
    docker exec evix-store-app-1 uv run python -c \
        "from app.tasks.es_sync import reindex_all; reindex_all()"
"""

from __future__ import annotations

import asyncio
import json
import urllib.request

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings


def _es(path: str) -> dict:
    with urllib.request.urlopen(f"{settings.elastic_url}{path}") as resp:
        return json.load(resp)


async def main() -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with maker() as s:
            pg_total = (await s.execute(text("select count(*) from product"))).scalar()
            pg_active = (
                await s.execute(text("select count(*) from product where is_active"))
            ).scalar()
            pg_ids = set(
                (await s.execute(text("select id from product"))).scalars().all()
            )

            idx = settings.elastic_index
            es_total = _es(f"/{idx}/_count")["count"]
            es_active = _es(f"/{idx}/_count?q=is_active:true")["count"]
            hits = _es(f"/{idx}/_search?size=10000&_source=false")["hits"]["hits"]
            es_ids = {int(h["_id"]) for h in hits}

            missing_es = sorted(pg_ids - es_ids)
            orphan_es = sorted(es_ids - pg_ids)

            print(f"PG products:       total={pg_total}  active={pg_active}")
            print(f"ES documents:      total={es_total}  active={es_active}")
            print(f"in PG, missing ES: count={len(missing_es)}  {missing_es[:20]}")
            print(f"in ES, orphaned:   count={len(orphan_es)}  {orphan_es[:20]}")
            in_sync = pg_total == es_total and not missing_es and not orphan_es
            print("verdict:", "IN SYNC ✓" if in_sync else "DRIFT ✗ — run reindex_all")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
