"""Search subsystem: Elasticsearch backend (ported ecom-elastic V3) + wiring.

The native Postgres FTS lives under ``app.repositories.search_repo`` /
``app.services.search_service`` and remains the fallback. This package holds the
ES index definition, document builder, query builder, async client and the
search backend that ``SearchService`` delegates to when ``search_backend=elastic``.
"""
