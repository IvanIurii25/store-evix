# Search subsystem — Elasticsearch + Postgres FTS fallback

> **TL;DR.** Storefront search runs on **Elasticsearch** (a port of the battle-tested
> `ecom-elastic` **V3** algorithm), with the native **Postgres FTS as an automatic
> fallback**. Which one serves is controlled by `SEARCH_BACKEND` (`elastic` | `postgres`);
> if ES is unreachable at runtime the service falls back to Postgres transparently, so
> search never goes dark. Single-tenant, languages **ru / ro**. Postgres is the source
> of truth; ES is a derived index kept in sync by Celery.

This document is the authoritative reference for how search is set up and how it works.
Any agent touching search should read this first.

---

## 1. Two backends, one endpoint

Everything is served by one endpoint — `GET /api/v1/search?q=&lang=ru|ro&page=&page_size=`
— and one service, `SearchService`. The service picks the backend:

```
             GET /api/v1/search
                    │
            SearchService.search()
                    │
        settings.search_uses_elastic ?
            ┌───────┴────────┐
         yes│                │ no  (or ES call raised → caught → fallback)
   EsSearchBackend      Postgres FTS  (search_repo.search_product_ids)
   (app/search/es/)     (native tsvector + ts_rank + code ILIKE)
            │                │
       [(id, score)] page    [(id, score)] ranked → sliced
            │                │
            └──────┬─────────┘
        load_cards(page_ids, lang)      ← shared hydration from product_card (§5.2)
                   │
        SearchResponse{data, total, page, page_size, suggestions}
```

- **ES down / timeout / error at runtime** → `SearchService.search` catches it, logs
  `es_search_failed … falling back to postgres`, and serves the Postgres result. A broken
  or empty index therefore never takes search down.
- Both branches hydrate the **same** `product_card` read-model, so the response shape and
  the displayed card (name/price/image) are identical regardless of backend. **The card
  name shown to users comes from `product_card`, not from the ES document** — ES only
  ranks ids.

---

## 2. Where things live (file map)

| File | Role |
|---|---|
| `app/api/routers/search.py` | HTTP endpoint (`/search`, `/catalog/categories/{slug}/facets`). Thin. |
| `app/services/search_service.py` | Backend selection + fallback. `_search_elastic` / `_search_postgres`. |
| `app/repositories/search_repo.py` | Postgres FTS queries + `load_cards` hydration (used by both backends). |
| `app/schemas/search.py` | `SearchResponse` (has `suggestions: list[str]`), `SearchHit`, facets. |
| **`app/search/es/index.py`** | ES index definition — analysis (char filters/analyzers) + field mappings. `INDEX_BODY`, `normalize_code`, `LANGS`. |
| **`app/search/es/query.py`** | The `function_score(bool)` query builder (`build_search_body`). |
| **`app/search/es/backend.py`** | `EsSearchBackend` — executes the query, returns `(id, score)` + total + suggestions, `health()`. |
| **`app/search/es/indexer.py`** | Builds ES docs from the ORM; `ensure_index` / `recreate_index` / `index_products` / `delete_products` / `reindex_all`. |
| **`app/search/es/client.py`** | Async ES client (shared singleton for the app; `new_es_client()` for Celery). |
| `app/tasks/es_sync.py` | Celery tasks: `search.index_products`, `search.delete_products`, `search.reindex_all`. |
| `app/services/admin_catalog_service.py` | Post-commit write-hooks that enqueue re-index/delete on product change. |
| `app/core/celery_app.py` | Registers `es_sync`; nightly `search.reindex_all` in `beat_schedule`. |
| `app/core/config.py` | `search_backend`, `elastic_url`, `elastic_index`, `elastic_min_score`, `elastic_request_timeout`. |
| `docker-compose.prod.yml` | `elasticsearch` service + `SEARCH_BACKEND` / `ELASTIC_*` env on app/worker/beat. |
| `scripts/es_sync_health.py` | Read-only PG↔ES consistency check. |
| `scripts/clean_html_entities.py` | One-shot: decode HTML entities in product names (import artifact). |
| `tests/search/test_es_query.py` | Unit tests for the index + query builders. |

Provenance: `/home/navi/projects/alsodev/ECOM/ecom-elastic` (the original multi-tenant
Django search service — `documents_v3.py`, `search/search_v3.py`, `.info/`). The porting
plan + journal lives in the vault: `проекты/evix/flystore/search-elastic-plan.md`.

---

## 3. The Elasticsearch index

Index name: `evix_products` (`ELASTIC_INDEX`). Single shard, zero replicas. Defined
entirely in `app/search/es/index.py` as `INDEX_BODY` (plain dicts — dumpable/validatable
standalone). **No ES plugins required** — the keyboard/transliteration magic uses
`char_filter` of `type: mapping`, not the ICU plugin (that was the old V2; V3 dropped it).

### 3.1 Analysis (what makes typo/layout/translit search work)

**char_filter (`type: mapping`):**
- `ru_to_en_keyboard` / `en_to_ru_keyboard` — ЙЦУКЕН ↔ QWERTY. Catches queries typed in the
  wrong layout (`,hjy[bghtn` → Бронхипрет).
- `translit_ru` — Cyrillic → Latin (`Бронхипрет` ↔ `bronhipret`).
- `ro_diacritics` — ă/î/ș/ț/â → a/i/s/t/a (Moldovan keyboards often omit diacritics:
  `incaltaminte` → `încălțăminte`).

**tokenizers:** `edge_ngram (2..15)` for name prefixes, `code_edge_ngram (2..20, +symbol/punct)`
for codes, `ngram_fallback (3..4)` for very short fragments.

**analyzers:** `standard_clean` (standard + lowercase + asciifolding — the base for
full-text), `edge_ngram_analyzer` (+ `edge_ngram_search` used at query time so the query
is not itself edge-ngrammed), `keyboard_ru/en_analyzer`, `translit_ru_analyzer`,
`ro_clean_analyzer`, `code_edge_ngram_analyzer` / `code_search_analyzer`, `ngram_fallback_*`.
`normalizer: lowercase_normalizer` for the exact keyword subfields.

Why **edge_ngram, not ngram**, on names: ngram(3,4) matched mid-word substrings (`"top"`
matched `"laptop"`) → huge noisy index. edge_ngram only matches word prefixes → far less
noise, smaller index.

### 3.2 Fields (per document = one product)

- `id` (long), `is_active` (bool — the search filter), `priority` (int — featured nudge).
- `code` (keyword) + `code.prefix` (code edge_ngram); `normalized_code` (keyword,
  separators stripped + uppercased via `normalize_code`).
- `name_{ru,ro}` (`standard_clean`) with sub-fields:
  - `.exact` (keyword + lowercase_normalizer) — literal-equality / starts-with tiers,
  - `.prefix` (edge_ngram) — "starts with",
  - `.ngram` (fallback) — short fragments,
  - **ru only:** `.keyboard`, `.translit`; **ro only:** `.clean` (diacritic fold).
- `category_{ru,ro}` — the product's category-path names joined (root..self).
- `attrs_{ru,ro}` — attribute values (the evix analogue of V3's curated `keywords`).
- `desc_{ru,ro}` — description, indexed at low boost for recall (V3 didn't index desc).
- `name_{ru,ro}_completion` — completion suggester source. Uses `standard_clean` analyzer
  (NOT the default `simple`, which drops digits: `"3d"` would collapse to `"d"` and suggest
  every name starting with D). **No completion context** (single-tenant).

---

## 4. The query (ranking)

Built by `build_search_body` (`app/search/es/query.py`). One `function_score(bool(should=[…]))`
— not the 6 sequential queries of the old V2. Every tier contributes additively; `priority`
is folded in via `field_value_factor(log1p)` so business priority nudges, never dominates,
BM25 relevance. `filter: is_active=true`. `minimum_should_match: 1`.

Tiers (high→low boost, adapted from V3 for evix's fields):

1. **Code** — `term code` 1000, `term normalized_code` 800, `code.prefix` 200/500,
   `prefix normalized_code`, optional `fuzzy normalized_code`.
2. **Name exact** — `term name.exact` 2000, `prefix name.exact` 800, `match_phrase name` 300.
3. **Name prefix** — `match name.prefix (and)` 250, `match_phrase_prefix name` 70 (multi-word
   typeahead tail), ro `.clean` phrase-prefix 65.
4. **Full-text** — `cross_fields (and)` 60 / `most_fields (and)` 40 (75% for 4+ words;
   3-word stopword safety net at 50); ro `.clean` diacritic net 55; `attrs` 40; `desc` 8.
5. **Category** — `category` best_fields 30, name+category cross_fields 25.
6. **Typo/layout** — `name_ru.keyboard` 15, `name_ro.clean` 15, `name_ru.translit` 10;
   `fuzzy AUTO` 30 and `ngram` 3 **only for single-token, non-code queries** (multi-token
   fuzzy caused cross-token noise, e.g. a TV's "s25" outranking "samsung s25" phones).

`exact_match=1` uses a reduced clause set (code + name.exact + name AND-match, no fuzzy/ngram).

**Dropped vs V3** (no backing field in evix): `additional_codes`, `group_product_id`,
`normalized_additional_codes`, brand + the brand+code combo, `custom_entity`. `keywords` →
`attrs`. Multi-tenant `group_id` filter removed entirely.

Suggestions: a completion suggester on `name_{lang}_completion` (prefix = the query),
merged across languages, order-preserving dedup, top 10. Returned in `SearchResponse.suggestions`
(empty on the Postgres path). **Note:** the current frontend does NOT render suggestions
(see §8) — it shows product hits directly.

`min_score` = `ELASTIC_MIN_SCORE` (currently `0` — no floor; raise once the score
distribution is known). ES round-trip capped at `elastic_request_timeout` (5 s) → timeout
falls back to Postgres.

---

## 5. Indexing & sync (Postgres → ES)

Postgres is the source of truth. ES is rebuilt from it — there is **no external feed** (the
original ecom-elastic pulled from a remote OBR DB with `feed_hash`/keyset machinery; none of
that exists here).

- **Incremental (fast path):** admin product writes enqueue a targeted re-index. Hooks in
  `admin_catalog_service` (`_enqueue_es_reindex` / `_enqueue_es_delete`) fire **post-commit**
  after create / update / translation / attributes / delete. They are lazily imported and
  swallow errors — a broker hiccup never fails a committed product write. The tasks no-op
  under the Postgres backend.
- **Nightly (reconciliation):** `search.reindex_all` in the Celery beat schedule rebuilds the
  whole index so it can never drift.
- **Document build** (`indexer._fetch_docs`): bulk queries (no N+1) join product +
  translations (name/desc) + category-path names + attribute values into one doc per product.

### Manual operations

```bash
# Full rebuild (recreate index with current mapping + index every product):
ssh evix-vpn 'docker exec evix-store-app-1 uv run python -c \
  "from app.tasks.es_sync import reindex_all; reindex_all()"'

# Check PG ↔ ES are in sync (read-only):
ssh evix-vpn 'docker exec evix-store-app-1 uv run python scripts/es_sync_health.py'

# Raw index inspection (ES is NOT exposed to the host — go via the container):
ssh evix-vpn 'docker exec evix-store-elasticsearch-1 curl -s localhost:9200/evix_products/_count'
ssh evix-vpn 'docker exec evix-store-elasticsearch-1 curl -s "localhost:9200/evix_products/_search?q=<term>&size=3"'
```

Any mapping change in `index.py` requires a redeploy of the app image **and** a `reindex_all`
(recreate drops + rebuilds the index).

---

## 6. Config & flags

| Setting (env) | Default | Meaning |
|---|---|---|
| `SEARCH_BACKEND` | `postgres` | `elastic` uses ES; `postgres` uses native FTS. Runtime ES failure falls back regardless. |
| `ELASTIC_URL` | `http://elasticsearch:9200` | ES cluster URL (internal compose network). |
| `ELASTIC_INDEX` | `evix_products` | Index name. |
| `ELASTIC_MIN_SCORE` | `0` | Relevance floor (0 = none). |
| `ELASTIC_REQUEST_TIMEOUT` | `5.0` | Per-round-trip cap (s); timeout → Postgres fallback. |

On prod these live in `~/apps/evix-store/.env` (interpolated by `docker-compose.prod.yml`).
**Rollout order matters:** deploy with `SEARCH_BACKEND=postgres`, run `reindex_all` to
populate the index, THEN set `SEARCH_BACKEND=elastic` and `docker compose up -d app worker beat`.

**Rollback (instant):** set `SEARCH_BACKEND=postgres` in `.env` + `docker compose up -d app`.

---

## 7. Infrastructure

`docker-compose.prod.yml` service `elasticsearch`:

- Image `docker.elastic.co/elasticsearch/elasticsearch:8.15.3`, `discovery.type=single-node`.
- **Security disabled** (`xpack.security.enabled=false`) — acceptable ONLY because the port
  is **never published to the host**; ES is reachable solely on the internal compose network.
  Do NOT publish 9200 or route it through Cloudflare without adding auth.
- Heap `-Xms512m -Xmx512m`, `mem_limit: 1400m`, `bootstrap.memory_lock`. Small catalog (~660
  products) → plenty. Volume `esdata`. curl-based healthcheck.
- Host prereq: `vm.max_map_count >= 262144` (the evix server is already at 1048576).
- app/worker/beat `depends_on: elasticsearch (service_started)` — deliberately NOT
  `service_healthy`, so a flaky ES can never block the app from starting (fallback covers it).

Deploy (documented flow): `git push` → `ssh evix-vpn 'cd ~/apps/evix-store && git pull --ff-only
&& docker compose -f docker-compose.prod.yml up -d --build app worker beat'` (+ `up -d elasticsearch`
the first time). See `проекты/evix/flystore/prod-deploy-progress.md` in the vault.

---

## 8. Frontend (evix-store-front)

- `src/islands/SearchBox.vue` — header live-search. On input (≥2 chars, 250 ms debounce)
  calls `/api/v1/search` and renders **product hits** (`data`: image + name + price, up to 6)
  as a dropdown + a "see all results" submit. This is a product-first autocomplete.
- `src/pages/[lang]/search.astro` — SSR full results page: `data` grid + pagination.
- `src/api/search.ts` — typed client.
- **`suggestions` is returned by the API and present in the generated types (`api.d.ts`)
  but is intentionally NOT rendered** — the product-first dropdown is the chosen UX. If you
  ever wire it, that's the place.

---

## 9. Known gaps / tuning knobs

- `ELASTIC_MIN_SCORE=0` — no relevance floor yet. Raise once the score distribution is known.
- `priority` currently only reflects `is_featured` (→ 10, else 0). Extend if richer business
  ranking is needed.
- Suggestions unused by the UI (see §8).
- Product **descriptions** legitimately contain HTML (`<p><img>`) — the entity-cleanup script
  only touches **names**; never unescape descriptions.

---

## 10. Tests

- `tests/search/test_es_query.py` — pure unit tests for `INDEX_BODY` (analyzers/subfields
  present) and `build_search_body` (tier structure, fuzzy single-token guard, exact mode,
  suggest, min_score). No ES/DB needed.
- `tests/search/integration/technical/` — the Postgres FTS path: `test_search.py`,
  `test_search_service.py`, `test_search_repo.py`, `test_search_router.py` (endpoint
  contract, ranking, facets).
- Live-ES behaviour (transliteration, typo, diacritic, ranking) is verified against prod
  after `reindex_all` (see the plan's regression checklist).
