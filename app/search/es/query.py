"""Search query builder — ported from ecom-elastic ``SearchV3``.

Builds ONE ``function_score(bool(should=[...]))`` body (not the 6 sequential
queries of V2). Each tier contributes additively; ``priority`` is folded in via
``field_value_factor(log1p)`` so business priority nudges, never dominates,
BM25 relevance.

Port deltas vs V3 (see index.py / plan §4):

* Languages ru/ro. No ``group_id`` filter (single-tenant).
* Dropped tiers with no backing field: ``additional_codes``,
  ``group_product_id``, ``normalized_additional_codes``, brand, brand+code combo,
  custom_entity.
* ``keywords`` tier → ``attrs_{lang}`` (attribute values); added a low-boost
  ``desc_{lang}`` fulltext tier for recall on the small catalog.
* Kept verbatim: exact/prefix/phrase name tiers, edge_ngram prefix, RO
  diacritic ``.clean`` net, keyboard/translit/fuzzy/ngram typo tiers, the
  3-word stopword safety net, and the single-token guard on fuzzy/ngram.
"""

from app.search.es.index import LANGS, normalize_code

# Code-tier prefix boosts. ``partially`` (prefix-heavy) raises them; both flags
# default off to mirror the V3 group defaults — flip via build_search_body args.
_PREFIX_BOOST_ON = 500
_PREFIX_BOOST_OFF = 200


def _code_should(q: str, *, normalize: bool, partially: bool, fuzzy: bool) -> list[dict]:
    """Code-matching clauses for a (sub)string ``q`` (V3 ``_code_should_clauses``)."""
    clauses: list[dict] = [
        {"term": {"code": {"value": q, "boost": 1000}}},
    ]
    norm = normalize_code(q)
    if normalize and norm:
        clauses.append({"term": {"normalized_code": {"value": norm, "boost": 800}}})

    prefix_boost = _PREFIX_BOOST_ON if partially else _PREFIX_BOOST_OFF
    clauses.append({"match": {"code.prefix": {"query": q, "boost": prefix_boost}}})
    if norm:
        clauses.append(
            {"prefix": {"normalized_code": {"value": norm, "boost": prefix_boost - 50}}}
        )
    if fuzzy and norm:
        clauses.append(
            {"fuzzy": {"normalized_code": {"value": norm, "fuzziness": 1, "boost": 300}}}
        )
    return clauses


def _name_exact_should(q: str) -> list[dict]:
    """Priority 2 — exact / prefix / phrase name tiers (V3 ``_build_name_exact``)."""
    q_lower = q.lower()
    clauses: list[dict] = []
    for lang in LANGS:
        clauses.append({"term": {f"name_{lang}.exact": {"value": q_lower, "boost": 2000}}})
        clauses.append({"prefix": {f"name_{lang}.exact": {"value": q_lower, "boost": 800}}})
        clauses.append({"match_phrase": {f"name_{lang}": {"query": q, "boost": 300}}})
    return clauses


def _name_prefix_should(q: str) -> list[dict]:
    """Priority 2.5 — edge_ngram prefix + multi-word typeahead tail (V3)."""
    clauses: list[dict] = []
    for lang in LANGS:
        clauses.append(
            {"match": {f"name_{lang}.prefix": {"query": q, "operator": "and", "boost": 250}}}
        )
    words = q.split()
    if len(words) >= 2 and len(words[-1]) >= 3:
        for lang in LANGS:
            clauses.append(
                {"match_phrase_prefix": {f"name_{lang}": {"query": q, "boost": 70, "max_expansions": 50}}}
            )
        clauses.append(
            {"match_phrase_prefix": {"name_ro.clean": {"query": q, "boost": 65, "max_expansions": 50}}}
        )
    return clauses


def _name_fulltext_should(q: str) -> list[dict]:
    """Priority 3 — full-text on names + RO diacritic net + attrs + desc (V3 adapt)."""
    name_fields = [f"name_{lang}^2" for lang in LANGS]
    word_count = len(q.split())

    if word_count <= 3:
        clauses: list[dict] = [
            {"multi_match": {"query": q, "fields": name_fields, "type": "cross_fields", "operator": "and", "boost": 60}},
            {"multi_match": {"query": q, "fields": name_fields, "type": "most_fields", "operator": "and", "boost": 40}},
        ]
    else:
        clauses = [
            {"multi_match": {"query": q, "fields": name_fields, "type": "cross_fields", "minimum_should_match": "75%", "boost": 60}},
            {"multi_match": {"query": q, "fields": name_fields, "type": "most_fields", "minimum_should_match": "75%", "boost": 40}},
        ]

    # 3-word stopword safety net (RO prepositions pentru/de/cu…): tolerate 1 miss.
    if word_count == 3:
        clauses.append(
            {"multi_match": {"query": q, "fields": name_fields, "type": "cross_fields", "minimum_should_match": "2", "boost": 50}}
        )
        clauses.append(
            {"multi_match": {"query": q, "fields": ["name_ro.clean^2"], "type": "cross_fields", "minimum_should_match": "2", "boost": 45}}
        )

    # Diacritic-insensitive Romanian match ("bucatarie" → "bucătărie").
    clauses.append(
        {"multi_match": {"query": q, "fields": ["name_ro.clean^2"], "type": "cross_fields", "operator": "and", "boost": 55}}
    )

    # Attribute values (evix analogue of V3 curated ``keywords``).
    attr_fields = [f"attrs_{lang}" for lang in LANGS]
    clauses.append(
        {"multi_match": {"query": q, "fields": attr_fields, "type": "best_fields", "operator": "and", "boost": 40}}
    )

    # Description — low boost, recall on the small catalog (V3 didn't index desc).
    desc_fields = [f"desc_{lang}" for lang in LANGS]
    clauses.append(
        {"multi_match": {"query": q, "fields": desc_fields, "type": "cross_fields", "operator": "and", "boost": 8}}
    )
    return clauses


def _category_should(q: str) -> list[dict]:
    """Priority 4 — category + cross name/category (V3 brand/category, brand dropped)."""
    category_fields = [f"category_{lang}" for lang in LANGS]
    name_cat_fields = [f"name_{lang}" for lang in LANGS] + category_fields
    return [
        {"multi_match": {"query": q, "fields": category_fields, "type": "best_fields", "operator": "and", "boost": 30}},
        {"multi_match": {"query": q, "fields": name_cat_fields, "type": "cross_fields", "operator": "and", "boost": 25}},
    ]


def _typo_layout_should(q: str) -> list[dict]:
    """Priority 5 — keyboard layout, transliteration, fuzzy, ngram (V3)."""
    clauses: list[dict] = []
    # Keyboard layout mismatch.
    clauses.append({"match": {"name_ru.keyboard": {"query": q, "operator": "and", "boost": 15}}})
    clauses.append({"match": {"name_ro.clean": {"query": q, "operator": "and", "boost": 15}}})
    # Transliteration (ru).
    clauses.append({"match": {"name_ru.translit": {"query": q, "operator": "and", "boost": 10}}})

    is_code_like = " " not in q and any(c.isdigit() for c in q)
    single_token = len(q.split()) == 1

    # Fuzzy — single-token text only (multi-token fuzzy caused the "samsung s25"
    # cross-token noise bug in prod; codes are matched exactly in the code tier).
    if single_token and not is_code_like:
        for lang in LANGS:
            clauses.append(
                {"match": {f"name_{lang}": {"query": q, "fuzziness": "AUTO", "operator": "and", "boost": 30}}}
            )
        ngram_fields = [f"name_{lang}.ngram" for lang in LANGS]
        clauses.append(
            {"multi_match": {"query": q, "fields": ngram_fields, "type": "most_fields", "minimum_should_match": "80%", "boost": 3}}
        )
    return clauses


def _wrap_function_score(should: list[dict]) -> dict:
    """Wrap should-clauses in bool + priority function_score (V3 scoring)."""
    return {
        "function_score": {
            "query": {"bool": {"should": should, "minimum_should_match": 1}},
            "functions": [
                {
                    "field_value_factor": {
                        "field": "priority",
                        "factor": 0.5,
                        "modifier": "log1p",
                        "missing": 0,
                    }
                }
            ],
            "boost_mode": "sum",
            "score_mode": "sum",
        }
    }


def build_search_body(
    query: str,
    *,
    size: int,
    from_: int = 0,
    exact_match: bool = False,
    min_score: float = 0.0,
    normalize_code_setting: bool = True,
    partially_search_by_code: bool = False,
    fuzzy_search_by_code: bool = False,
    suggest_prefix: str | None = None,
) -> dict:
    """Assemble the full ES ``_search`` request body.

    Args:
        query: The (already-stripped, non-empty) user query.
        size / from_: Pagination window.
        exact_match: Use the reduced exact-match clause set (no fuzzy/ngram).
        min_score: ES relevance floor (0 = none).
        normalize_code_setting / partially_search_by_code / fuzzy_search_by_code:
            Code-tier behaviour flags (single-tenant constants, args for tests).
        suggest_prefix: When set, attach a completion suggester on this prefix.

    Returns:
        A dict ready to pass as the ES ``body`` (``_source`` limited to ``id``).
    """
    q = query.strip()
    filter_clauses = [{"term": {"is_active": True}}]

    if exact_match:
        q_lower = q.lower()
        norm = normalize_code(q)
        should: list[dict] = [
            {"term": {"code": {"value": q, "boost": 1000}}},
        ]
        if norm:
            should.append({"term": {"normalized_code": {"value": norm, "boost": 800}}})
        for lang in LANGS:
            should.append({"term": {f"name_{lang}.exact": {"value": q_lower, "boost": 300}}})
            should.append({"multi_match": {"query": q, "fields": [f"name_{lang}"], "operator": "and", "boost": 80}})
        main_query = {"bool": {"should": should, "minimum_should_match": 1}}
    else:
        should = (
            _code_should(q, normalize=normalize_code_setting, partially=partially_search_by_code, fuzzy=fuzzy_search_by_code)
            + _name_exact_should(q)
            + _name_prefix_should(q)
            + _name_fulltext_should(q)
            + _category_should(q)
            + _typo_layout_should(q)
        )
        main_query = _wrap_function_score(should)

    body: dict = {
        "query": {"bool": {"filter": filter_clauses, "must": [main_query]}},
        "from": from_,
        "size": size,
        "_source": ["id"],
    }
    if min_score and min_score > 0:
        body["min_score"] = min_score
    if suggest_prefix:
        body["suggest"] = {
            f"name_{lang}_suggest": {
                "prefix": suggest_prefix.lower(),
                "completion": {"field": f"name_{lang}_completion", "size": 10, "skip_duplicates": True},
            }
            for lang in LANGS
        }
    return body
