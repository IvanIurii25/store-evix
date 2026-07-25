"""Celery tasks that keep the Elasticsearch index in sync with Postgres.

* ``search.index_products`` / ``search.delete_products`` — targeted incremental
  updates, enqueued from the admin write path after a product changes. Gated on
  ``search_backend == elastic`` (no-op under the Postgres backend).
* ``search.reindex_all`` — full rebuild (recreate index + index every product).
  NOT gated: it is the tool used to populate the index before flipping the
  backend to ``elastic``, and the nightly reconciliation sweep.

DB work uses :func:`app.tasks.base.run_async_session` (fork-safe NullPool engine
on a fresh loop); the ES client is created and closed inside the same coroutine.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.celery_app import celery_app
from app.core.config import settings
from app.search.es import indexer
from app.search.es.client import new_es_client
from app.tasks.base import run_async_session

logger = logging.getLogger(__name__)


async def _index(session: AsyncSession, product_ids: list[int]) -> int:
    client = new_es_client()
    try:
        await indexer.ensure_index(client)
        return await indexer.index_products(session, client, product_ids)
    finally:
        await client.close()


async def _delete(product_ids: list[int]) -> int:
    client = new_es_client()
    try:
        await indexer.delete_products(client, product_ids)
        return len(product_ids)
    finally:
        await client.close()


async def _reindex(session: AsyncSession) -> int:
    client = new_es_client()
    try:
        return await indexer.reindex_all(session, client)
    finally:
        await client.close()


@celery_app.task(
    name="search.index_products",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def index_products(product_ids: list[int]) -> int:
    """(Re)index the given products in ES. No-op under the Postgres backend."""
    if not settings.search_uses_elastic or not product_ids:
        return 0
    count = run_async_session(lambda s: _index(s, product_ids))
    logger.info("es_index_products ids=%s indexed=%d", product_ids, count)
    return count


@celery_app.task(
    name="search.delete_products",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def delete_products(product_ids: list[int]) -> int:
    """Remove the given products from ES. No-op under the Postgres backend."""
    if not settings.search_uses_elastic or not product_ids:
        return 0
    count = run_async_session(lambda _s: _delete(product_ids))
    logger.info("es_delete_products ids=%s", product_ids)
    return count


@celery_app.task(name="search.reindex_all")
def reindex_all() -> int:
    """Recreate the index and index every product. Returns docs indexed."""
    total = run_async_session(_reindex)
    logger.info("es_reindex_all indexed=%d", total)
    return total
