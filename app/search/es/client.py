"""Async Elasticsearch client provider.

A single :class:`AsyncElasticsearch` is shared per process (it manages its own
aiohttp connection pool). The FastAPI app closes it on shutdown; Celery tasks
build and close their own client per run (fork-safe, mirroring the DB pattern in
:mod:`app.tasks.base`).
"""

from elasticsearch import AsyncElasticsearch

from app.core.config import settings

_client: AsyncElasticsearch | None = None


def get_es_client() -> AsyncElasticsearch:
    """Return the process-wide async ES client (lazy singleton).

    Used on the request path (the app owns the lifecycle and closes it on
    shutdown via :func:`close_es_client`).
    """
    global _client
    if _client is None:
        _client = AsyncElasticsearch(
            hosts=[settings.elastic_url],
            request_timeout=settings.elastic_request_timeout,
            max_retries=1,
            retry_on_timeout=False,
        )
    return _client


async def close_es_client() -> None:
    """Close and drop the shared client (FastAPI shutdown hook)."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def new_es_client() -> AsyncElasticsearch:
    """Build a fresh, independently-owned client (for Celery tasks).

    The caller MUST ``await client.close()`` when done — do not reuse the shared
    singleton in a prefork worker (its pool is not fork-safe).
    """
    return AsyncElasticsearch(
        hosts=[settings.elastic_url],
        request_timeout=settings.elastic_request_timeout,
        max_retries=1,
        retry_on_timeout=False,
    )
