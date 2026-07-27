"""Elasticsearch search backend (ported from ecom-elastic V3).

Modules:

* :mod:`app.search.es.index` — index settings (analyzers/char-filters) + mappings.
* :mod:`app.search.es.client` — async ES client singleton.
* :mod:`app.search.es.indexer` — build ES documents from ORM + (re)index/delete.
* :mod:`app.search.es.query` — the ``function_score(bool)`` query builder.
* :mod:`app.search.es.backend` — ``EsSearchBackend`` (rank ids + suggest).

Design deltas vs the ecom-elastic V3 source (see search-elastic-plan.md §4):
single-tenant (no ``group_id``), languages ru/ro only, no external HTTP feed
(local Postgres is the source of truth), no ICU plugin (char_filter ``mapping``),
raw ``AsyncElasticsearch`` client instead of ``django-elasticsearch-dsl``.
"""
