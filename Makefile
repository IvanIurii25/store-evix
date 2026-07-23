.PHONY: help sync dev lint format format-check check test test-cov test-file \
        migrate migration downgrade seed seed-bulk bench create-staff hooks \
        up down logs build image-run clean

# Host port for the local dev server (:8000 may clash with other local apps).
PORT ?= 8000
# Infra containers the test-suite and dev server need.
SERVICES = db redis minio mailhog

help: ## Show this help
	@echo "evix-store — commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- Setup / run ---
sync: ## Install/sync dependencies (uv)
	uv sync

dev: ## Run dev server with hot reload (PORT=8000 by default)
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port $(PORT)

# --- Quality ---
lint: ## Lint with ruff
	uv run ruff check .

format: ## Auto-format with ruff
	uv run ruff format .

format-check: ## Check formatting without writing (CI)
	uv run ruff format --check .

check: lint format-check test ## Lint + format-check + tests (local CI gate)

# --- Tests ---
test: ## Run the full test suite (needs infra: make up)
	uv run pytest -q

test-cov: ## Run tests with coverage report (fails below 90%)
	uv run pytest --cov --cov-report=term-missing --cov-fail-under=90

test-file: ## Run one test path (usage: make test-file file=tests/auth)
	uv run pytest $(file) -v

# --- Database ---
migrate: ## Apply migrations to head
	uv run alembic upgrade head

migration: ## Autogenerate a migration (usage: make migration m="add x")
	uv run alembic revision --autogenerate -m "$(or $(m),auto)"

downgrade: ## Downgrade one revision
	uv run alembic downgrade -1

# --- Data / ops scripts ---
seed: ## Seed a small demo catalog
	uv run python scripts/seed.py

seed-bulk: ## Seed a bulk catalog (usage: make seed-bulk count=2000)
	EVIX_SEED_COUNT=$(or $(count),2000) uv run python scripts/seed_bulk.py

bench: ## Benchmark hot paths (usage: make bench p95=200)
	EVIX_BENCH_P95_MS=$(or $(p95),0) uv run python scripts/bench.py

create-staff: ## Create/promote a staff user (usage: make create-staff email=a@b.md password=***)
	uv run python scripts/create_staff.py --email $(email) --password $(password)

hooks: ## Install git pre-commit hooks
	uv run pre-commit install

# --- Docker ---
up: ## Start infra containers (db, redis, minio, mailhog) + init bucket
	docker compose up -d $(SERVICES)
	docker compose up minio-init

down: ## Stop all containers
	docker compose down

logs: ## Tail app container logs
	docker compose logs -f app

build: ## Build the app Docker image
	docker compose build app

image-run: ## Run the full stack incl. app container
	docker compose up -d

clean: ## Stop containers and remove volumes
	docker compose down -v --remove-orphans
