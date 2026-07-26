# evix-store (backend)

Single-tenant e-commerce core — **storefront + back-office**, feature-complete and
MVP-hardened. FastAPI + async SQLAlchemy 2.0.

Sequential path works end-to-end: catalog (tree / breadcrumbs / cursor listing /
PDP) → search (Elasticsearch, ported ecom-elastic V3, with Postgres FTS fallback —
ru/ro; see [docs/SEARCH.md](docs/SEARCH.md)) + facets → cart (guest + user, merge) →
checkout (COD, atomic, race-safe stock) → order (state machine, guest lookup).
Auth (JWT + argon2 + Redis revocation), thin admin-API, media on MinIO/S3, order
e-mail, rate limiting. **152 tests.**

**Stack:** Python 3.12 · FastAPI · async SQLAlchemy 2.0 (asyncpg) · Pydantic v2 +
pydantic-settings · Alembic · orjson · PostgreSQL 16 · Redis 7 · Elasticsearch 8 ·
MinIO/S3 · argon2 · uv · Docker. Tooling: ruff (lint + format), pytest (+cov), pre-commit, CI.

---

## Quick start

```bash
cp .env.example .env
make sync                 # install deps (uv)
make up                   # start infra: db, redis, minio, mailhog (+ bucket)
make create-staff email=admin@evix.md password=secret   # first admin
make seed                 # small demo catalog
make dev PORT=58000       # dev server (→ http://localhost:58000/api/v1/health)
```

Run `make help` to list every command.

## Commands (Makefile)

| Command | What it does |
|---|---|
| `make help` | List all commands (self-documenting) |
| `make sync` | Install/sync dependencies (uv) |
| `make dev` `[PORT=]` | Dev server with hot reload (`PORT=8000` default) |
| `make lint` | Lint with ruff |
| `make format` / `make format-check` | Auto-format / check-only (CI) |
| `make check` | Lint + format-check + tests (local CI gate) |
| `make test` | Full test suite (needs `make up`) |
| `make test-cov` | Tests with coverage report |
| `make test-file file=tests/auth` | Run one path |
| `make migrate` | Apply migrations to head |
| `make migration m="add x"` | Autogenerate a migration |
| `make downgrade` | Downgrade one revision |
| `make seed` / `make seed-bulk [count=2000]` | Seed demo / bulk catalog |
| `make bench [p95=200]` | Benchmark hot paths (perf gate) |
| `make create-staff email= password=` | Create/promote a staff user |
| `make hooks` | Install git pre-commit hooks |
| `make up` / `make down` / `make logs` | Infra up / all down / tail app logs |
| `make build` / `make image-run` | Build app image / run full stack incl. app |
| `make clean` | Stop containers + remove volumes |

## Host ports (dev)

Non-default to avoid clashing with other local projects:

| Service | Host port | Note |
|---|---|---|
| app (docker) | `58000` | `:8000` occupied by another local app |
| PostgreSQL | `55432` | |
| Redis | `56379` | |
| MinIO | `59000` (API) / `59001` (console) | |
| MailHog | `51025` (SMTP) / `58025` (UI) | |

The test harness (`tests/conftest.py`) connects to these ports; `make up` provides them.

---

## Layout

```
app/
  main.py                  # app factory: logging, CORS, routers, media mount, lifespan
  core/                    # config, db, errors, pagination, security, redis, storage, email, logging
  models/                  # SQLAlchemy 2.0 models (catalog, user, cart, order, promo) + base
  schemas/                 # Pydantic v2 request/response
  repositories/            # async SQL (no business logic)
  services/                # business logic + transactions
  api/
    deps.py                # current_user / current_staff / guest_or_user
    ratelimit.py           # Redis fixed-window limiter
    routers/               # catalog, search, cart, checkout, orders, auth, users, admin_*
alembic/                   # async env + migrations (domain schema, order_number_seq)
scripts/                   # seed, seed_bulk, bench, create_staff
tests/                     # per-domain suites + shared harness (isolated DB + tx rollback)
```

## Conventions

- **API version:** everything under `/api/v1`.
- **Errors:** `{"error": {"code": "...", "message": "...", "details": ...}}` — no stack
  traces leaked; a domain code flows via `raise HTTPException(status, detail={"code","message"})`.
- **Pagination:** listings use a **cursor** envelope `{data, next_cursor}` (keyset, no deep
  OFFSET); admin/search lists use `{data, total, page, page_size}`.
- **i18n:** ru + ro via `*_translation` tables; a product/category is only publishable
  when both languages exist.
- **Auth:** JWT access+refresh in httpOnly cookies (or `Authorization: Bearer`); refresh
  rotation + Redis blacklist.

## Dev tooling

Adopted from the SmartSuggest project (best practices, minus its root-script clutter):

- **ruff** — lint (`make lint`) and format (`make format`); config in `pyproject.toml`
  (line length owned by the formatter). Everything is formatted and lint-clean.
- **pytest + coverage** — `make test` / `make test-cov`. Isolated per-test Postgres
  transaction (SAVEPOINT rollback) + dedicated test DB per domain.
- **pre-commit** — ruff (fix + format) + hygiene hooks; `make hooks` to install.
- **CI** — `.github/workflows/ci.yml`: ruff + full suite against compose infra.
- **Structured logging** — `app/core/logging.py` (`setup_logging()`), level from
  `LOG_LEVEL`.

Deployment/operations: see the vault ops runbook (`проекты/evix/flystore/ops.md`).
