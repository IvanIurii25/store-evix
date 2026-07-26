# Deploy branch & coordination policy

Authoritative rules for **what reaches production** and how parallel sessions
avoid shipping half-built features. Read this before pushing to `main` or deploying.

## origin/main = the deploy branch

The prod server (`evix` MSI, `~/apps/evix-store`) deploys by pulling this branch:

```bash
ssh evix-vpn 'cd ~/apps/evix-store && git pull --ff-only && \
  docker compose -f docker-compose.prod.yml up -d --build app worker'
```

**Therefore: anything on `origin/main` can go live on the very next deploy** — even
a deploy triggered for an unrelated change (the server pulls the *whole* branch, not
one commit). Keep `origin/main` **always deployable**.

### Hard rules
1. **Do NOT push an unfinished feature to `origin/main`.** Land WIP as *local* commits
   or on a feature branch; push to `origin/main` only when the feature is
   end-to-end ready (or is provably inert — additive schema with no code path/data).
2. **Before deploying, verify `origin/main` HEAD is a known-good commit.** A `git pull`
   carries every commit that landed since the server's last deploy.
3. **When a Celery task module changes, rebuild `app` AND `worker` together.** `app`
   enqueues; `worker` must have the new task registered or the job dead-letters.
4. Migrations run via the `migrate` service (`alembic upgrade head`) automatically on
   `up -d`; confirm `alembic current` == expected head after deploy.

## Current coordination state (2026-07-26)

Two sessions share this working copy. Live divergence:

- **`origin/main` (= in prod):** up to `bda024e` (checkout email→Celery + row-lock tuning).
  Variants feature in prod = **P0 only** (`ee70710`, migration `b8e1f4a2c9d3`) — **inert**:
  empty tables (`product_variant`=0, `has_variants`=0 products), API exposes no variant
  fields. Zero behavior change. Landed unintentionally via a `git pull` on a lagging
  server; safe, nothing to roll back.
- **Local `main` is AHEAD of `origin/main` by 2 unpushed commits:**
  - `429105b` — P1 variant read path
  - `37fe078` — P2a variant buy-path schema (migration `c9d2e5f7a1b4`)

### 🔴 Do not push local `main` to origin yet
P1/P2a alone put unused `variant_id` columns and read code on the deploy branch while
the **buy path (P2b–f), admin CRUD, storefront UI (P3/P4) and data backfill (P5) are
not done**. P2 is flagged high-risk (stock/race). Hold `origin/main` at `bda024e` until
variants are E2E-ready, then push the full, tested set in one go.

Phase-by-phase status & the full snapshot:
`PersonalAssistant/проекты/evix/flystore/variants-plan.md` → "Прогресс по фазам".
