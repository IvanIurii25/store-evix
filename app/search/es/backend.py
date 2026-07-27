"""Elasticsearch search backend — thin execution layer over the query builder.

Owns the round-trip only: build the body (:mod:`app.search.es.query`), execute
against the shared async client, and shape the response into ``(id, score)``
pairs + total + suggestion strings. Business rules (pagination maths, card
hydration, fallback decision) stay in :class:`app.services.search_service`.
"""

import logging

from app.core.config import settings
from app.search.es.client import get_es_client
from app.search.es.index import LANGS
from app.search.es.query import build_search_body

logger = logging.getLogger(__name__)


class EsSearchResult:
    """Ranked page from ES: ``(id, score)`` pairs, total, suggestion strings."""

    __slots__ = ("hits", "total", "suggestions")

    def __init__(
        self,
        hits: list[tuple[int, float]],
        total: int,
        suggestions: list[str],
    ) -> None:
        self.hits = hits
        self.total = total
        self.suggestions = suggestions


class EsSearchBackend:
    """Execute one search request against Elasticsearch."""

    async def search(
        self,
        query: str,
        *,
        page: int,
        page_size: int,
        exact_match: bool = False,
        with_suggestions: bool = True,
    ) -> EsSearchResult:
        """Run a ranked search and return one page of product ids + suggestions.

        Args:
            query: Non-empty user query (caller guarantees non-blank).
            page: 1-based page number.
            page_size: Page size (already clamped by the caller).
            exact_match: Reduced exact-match clause set.
            with_suggestions: Attach a completion suggester on the query prefix.

        Returns:
            EsSearchResult: page hits ``(id, score)``, total, suggestion texts.

        Raises:
            Exception: propagates ES/transport errors so the service can fall
                back to Postgres FTS.
        """
        from_ = (page - 1) * page_size
        body = build_search_body(
            query,
            size=page_size,
            from_=from_,
            exact_match=exact_match,
            min_score=settings.elastic_min_score,
            suggest_prefix=query if with_suggestions else None,
        )
        client = get_es_client()
        try:
            resp = await client.search(index=settings.elastic_index, body=body)
        except Exception:
            if with_suggestions:
                # Suggester can fail independently (e.g. empty completion field);
                # retry once without it before giving up to the fallback.
                body.pop("suggest", None)
                resp = await client.search(index=settings.elastic_index, body=body)
            else:
                raise

        hits: list[tuple[int, float]] = []
        for hit in resp["hits"]["hits"]:
            pid = hit.get("_source", {}).get("id")
            if pid is not None:
                hits.append((int(pid), float(hit.get("_score") or 0.0)))

        total_obj = resp["hits"]["total"]
        total = total_obj["value"] if isinstance(total_obj, dict) else int(total_obj)

        suggestions = self._collect_suggestions(resp)
        return EsSearchResult(hits=hits, total=total, suggestions=suggestions)

    @staticmethod
    def _collect_suggestions(resp: dict) -> list[str]:
        """Merge per-language completion options, dedup preserving order."""
        suggest = resp.get("suggest")
        if not suggest:
            return []
        texts: list[str] = []
        for lang in LANGS:
            key = f"name_{lang}_suggest"
            for block in suggest.get(key, []):
                for option in block.get("options", []):
                    text = option.get("text")
                    if text:
                        texts.append(text)
        # Order-preserving dedup (V3 WEAK-7 fix: dict.fromkeys, not set()).
        return list(dict.fromkeys(texts))[:10]
