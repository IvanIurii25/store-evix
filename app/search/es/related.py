"""Related-products candidate ranking via Elasticsearch ``more_like_this``.

The storefront "similar products" rail wants genuine content similarity, not the
"newest in the same category" heuristic the Postgres path gives. ES already
indexes everything needed per product — ``name_{lang}``, ``attrs_{lang}``
(attribute values: colour/size/material/type), ``category_{lang}`` (the category
path names) and ``desc_{lang}`` — so a ``more_like_this`` query over those fields
returns products sharing the most *distinctive* terms with the seed product.

Cross-category by design: MLT scores any product sharing terms, but ``category``
carries a boost so a same-category/department sibling still outranks a loosely
related item from elsewhere (the "boost own category" rule). Stock/price are NOT
in ES (stock is volatile); this layer only *ranks* — the service hydrates the
ranked ids from ``product_card`` and applies the authoritative in-stock filter,
mirroring the search pipeline (ES ranks ids → Postgres is the source of truth).
"""

from app.core.config import settings
from app.search.es.client import get_es_client

# MLT field boosts: attribute/name terms are the most discriminating for
# "similar", category keeps results in the same department (boost own category),
# description is a low-weight recall net.
_LIKE_FIELD_BOOSTS: dict[str, int] = {
    "name": 4,
    "attrs": 3,
    "category": 2,
    "desc": 1,
}


def build_related_body(product_id: int, lang: str, *, size: int) -> dict:
    """Assemble the ES ``more_like_this`` body ranking products similar to a seed.

    Args:
        product_id: Seed product whose ES document drives the ``like`` clause.
        lang: Request language — selects the ``*_{lang}`` field variants.
        size: Number of candidate ids to return (the service over-fetches, since
            some candidates get dropped by the authoritative in-stock filter).

    Returns:
        A dict ready to pass as the ES ``body`` (``_source`` limited to ``id``).

    Note:
        The MLT term-frequency floors are relaxed to ``1`` on purpose: the
        catalogue is small (hundreds of docs), and ES's defaults
        (``min_term_freq=2``, ``min_doc_freq=5``) would discard most terms and
        return nothing. ``minimum_should_match`` keeps precision up.
    """
    like_fields = [
        f"{field}_{lang}^{boost}" for field, boost in _LIKE_FIELD_BOOSTS.items()
    ]
    return {
        "query": {
            "bool": {
                "must": [
                    {
                        "more_like_this": {
                            "fields": like_fields,
                            "like": [
                                {
                                    "_index": settings.elastic_index,
                                    "_id": str(product_id),
                                }
                            ],
                            "min_term_freq": 1,
                            "min_doc_freq": 1,
                            "max_query_terms": 25,
                            "minimum_should_match": "30%",
                            "include": False,  # never return the seed itself
                        }
                    }
                ],
                "filter": [{"term": {"is_active": True}}],
                # Belt-and-suspenders: MLT ``include=False`` already drops the
                # seed, but exclude by id too in case it slips through analysis.
                "must_not": [{"ids": {"values": [str(product_id)]}}],
            }
        },
        "size": size,
        "_source": ["id"],
    }


async def fetch_related_ids(product_id: int, lang: str, size: int) -> list[int]:
    """Return content-similar product ids for a seed, ranked by ES relevance.

    Args:
        product_id: Seed product id (must exist in the index; a missing doc just
            yields an empty list, which the service tops up).
        lang: Request language code.
        size: Maximum number of candidate ids to fetch.

    Returns:
        list[int]: Candidate product ids, most-similar first (self excluded).

    Raises:
        Exception: propagates ES/transport errors so the service can fall back to
            the Postgres same-category path (never leaves the rail dark).
    """
    body = build_related_body(product_id, lang, size=size)
    client = get_es_client()
    resp = await client.search(index=settings.elastic_index, body=body)
    ids: list[int] = []
    for hit in resp["hits"]["hits"]:
        pid = hit.get("_source", {}).get("id")
        if pid is not None:
            ids.append(int(pid))
    return ids
