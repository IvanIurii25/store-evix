# Contributing

`main` is protected: **no direct pushes** — every change lands through a pull
request with a green CI (`quality`) check.

## Workflow

1. Branch from an up-to-date `main`:

   ```bash
   git checkout main && git pull
   git checkout -b <type>/<short-desc>   # e.g. feat/promo-codes
   ```

2. Make your change. Keep commits focused and use Conventional Commit messages
   (`feat:`, `fix:`, `test:`, `docs:`, `ci:`, `style:`, `refactor:`, `chore:`).

3. Run the checks locally (mirror CI) so the PR passes first time — see below.

4. Push and open a PR:

   ```bash
   git push -u origin <branch>
   gh pr create --base main --fill
   ```

5. CI runs on the PR. When **quality** is green, merge (no approval required):

   ```bash
   gh pr merge --squash --delete-branch
   ```

   `git push origin main` is rejected by branch protection — always go through a PR.

## Local checks (run before pushing)

Bring up the test infra once, then run the gate:

```bash
make up      # Postgres + Redis (+ MinIO/MailHog)
make check   # ruff lint + format-check + tests
```

Or piecemeal:

```bash
uv run ruff check .
uv run ruff format .
uv run pytest --cov --cov-report=term-missing --cov-fail-under=90
```

CI runs the same **quality** job: `ruff check .` + `ruff format --check .` +
the pytest coverage gate. MinIO/MailHog aren't in CI, so 4 S3/SMTP integration
tests skip there — `make up` locally exercises them.

## Tests

New code needs tests — total coverage must stay **≥90%** (CI fails below). Follow
the existing layout under `tests/` (`unit/`, `integration/{technical,business}/`)
and build fixtures with the factories in `tests/factories.py`.
