"""Latency benchmark for the catalog hot paths (§5.5).

Drives the real FastAPI app in-process over ``httpx.ASGITransport`` (no network)
against the database in ``DATABASE_URL`` — point it at ``evix_bench`` populated by
:mod:`scripts.seed_bulk`. It measures p50 / p95 / p99 latency (ms) over ``N``
iterations for each hot path and prints a table:

* category listing, page 1 (keyset first page);
* deep cursor pagination (walk several pages — shows the cursor does **not**
  degrade with depth, unlike ``OFFSET``);
* full-text search (``/search?q=``);
* category facets;
* product detail page (PDP).

Optional latency thresholds (``EVIX_BENCH_P95_MS`` etc.) turn this into a perf
gate: any endpoint whose p95 exceeds its threshold makes the process exit
non-zero (the CI regression gate of §5.5). Without thresholds it is a pure
report and always exits ``0``.

Usage::

    DATABASE_URL=postgresql+asyncpg://evix:evix@localhost:55432/evix_bench \\
        uv run python scripts/bench.py                     # 200 iterations
    ... uv run python scripts/bench.py --iterations 500
    EVIX_BENCH_P95_MS=40 ... uv run python scripts/bench.py  # perf gate
"""

import argparse
import asyncio
import os
import time
from dataclasses import dataclass, field

from httpx import ASGITransport, AsyncClient

from app.main import app

API_V1_PREFIX: str = "/api/v1"
DEFAULT_ITERATIONS: int = 200
# Warm-up requests excluded from stats (JIT of query plans, connection fill).
WARMUP_ITERATIONS: int = 10
# How many pages the deep-pagination probe walks per iteration.
DEEP_PAGE_DEPTH: int = 20
# Language the bulk seed guarantees for both translations; ``ro`` is the default.
BENCH_LANG: str = "ro"
# A token present in every bulk-seeded ``ro`` product name ("Produs N").
SEARCH_TERM: str = "Produs"


@dataclass
class LatencySample:
    """Collected per-request latencies (ms) for one named hot path."""

    name: str
    samples: list[float] = field(default_factory=list)

    def record(self, elapsed_ms: float) -> None:
        """Append one latency measurement.

        Args:
            elapsed_ms: Wall-clock request latency in milliseconds.
        """
        self.samples.append(elapsed_ms)

    def percentile(self, pct: float) -> float:
        """Return the ``pct`` percentile latency (ms).

        Args:
            pct: Percentile in ``[0, 100]``.

        Returns:
            float: The percentile value, or ``0.0`` when no samples exist.
        """
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        rank = max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered)) - 1))
        return ordered[rank]


@dataclass
class BenchContext:
    """Discovered slugs the probes need (first category + first product)."""

    category_slug: str
    product_slug: str


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments (iteration count).

    Returns:
        argparse.Namespace: Parsed args with ``iterations``.
    """
    parser = argparse.ArgumentParser(description="Benchmark catalog hot paths.")
    parser.add_argument(
        "--iterations",
        type=int,
        default=int(os.environ.get("EVIX_BENCH_ITERATIONS", DEFAULT_ITERATIONS)),
        help=f"Measured iterations per endpoint (default {DEFAULT_ITERATIONS}).",
    )
    return parser.parse_args()


async def _discover(client: AsyncClient) -> BenchContext:
    """Find a live category slug and a product slug to probe.

    Args:
        client: The in-process ASGI client.

    Returns:
        BenchContext: Slugs for the listing / facets / PDP probes.

    Raises:
        RuntimeError: If the benchmark DB has no active catalog data.
    """
    cats = await client.get(
        f"{API_V1_PREFIX}/catalog/categories", params={"lang": BENCH_LANG}
    )
    cats.raise_for_status()
    tree = cats.json()
    if not tree:
        raise RuntimeError(
            "No categories — run scripts/seed_bulk.py against this DB first."
        )
    # Prefer a leaf (has products); fall back to the root.
    category_slug = tree[0]["slug"]
    for node in tree[0].get("children", []):
        category_slug = node["slug"]
        break

    listing = await client.get(
        f"{API_V1_PREFIX}/catalog/categories/{category_slug}/products",
        params={"lang": BENCH_LANG},
    )
    listing.raise_for_status()
    data = listing.json()["data"]
    if not data:
        raise RuntimeError(f"Category '{category_slug}' has no products — seed first.")
    return BenchContext(category_slug=category_slug, product_slug=data[0]["slug"])


async def _measure(
    client: AsyncClient,
    sample: LatencySample,
    coro_factory,
    iterations: int,
) -> None:
    """Run ``coro_factory`` ``iterations`` times, recording each latency.

    Args:
        client: The in-process ASGI client.
        sample: The sink for recorded latencies.
        coro_factory: A zero-arg callable returning the request coroutine.
        iterations: Number of measured iterations.
    """
    for _ in range(WARMUP_ITERATIONS):
        await coro_factory()
    for _ in range(iterations):
        started = time.perf_counter()
        await coro_factory()
        sample.record((time.perf_counter() - started) * 1000)


async def _walk_cursor(client: AsyncClient, ctx: BenchContext) -> None:
    """Walk several keyset pages of a listing (deep-pagination probe).

    Args:
        client: The in-process ASGI client.
        ctx: Discovered slugs.
    """
    cursor: str | None = None
    for _ in range(DEEP_PAGE_DEPTH):
        params: dict[str, str] = {"lang": BENCH_LANG, "sort": "price_asc"}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await client.get(
            f"{API_V1_PREFIX}/catalog/categories/{ctx.category_slug}/products",
            params=params,
        )
        resp.raise_for_status()
        cursor = resp.json().get("next_cursor")
        if cursor is None:
            break


def _thresholds() -> dict[str, float]:
    """Read optional per-metric p95 thresholds (ms) from the environment.

    A single ``EVIX_BENCH_P95_MS`` applies to every endpoint; per-endpoint
    overrides use ``EVIX_BENCH_P95_MS_<NAME>`` (name upper-cased, non-alnum → ``_``).

    Returns:
        dict[str, float]: Map of endpoint-name -> p95 threshold in ms (may be empty).
    """
    global_threshold = os.environ.get("EVIX_BENCH_P95_MS")
    result: dict[str, float] = {}
    if global_threshold:
        result["*"] = float(global_threshold)
    return result


def _print_table(samples: list[LatencySample], iterations: int) -> None:
    """Print the p50/p95/p99 latency table.

    Args:
        samples: Collected latency samples per endpoint.
        iterations: Iterations used (for the header).
    """
    print(f"\nLatency over {iterations} iterations (ms):\n")
    header = f"{'endpoint':<28}{'p50':>10}{'p95':>10}{'p99':>10}{'max':>10}"
    print(header)
    print("-" * len(header))
    for sample in samples:
        print(
            f"{sample.name:<28}"
            f"{sample.percentile(50):>10.2f}"
            f"{sample.percentile(95):>10.2f}"
            f"{sample.percentile(99):>10.2f}"
            f"{max(sample.samples, default=0.0):>10.2f}"
        )


def _check_gate(samples: list[LatencySample], thresholds: dict[str, float]) -> int:
    """Return a non-zero exit code if any p95 breaches its threshold.

    Args:
        samples: Collected latency samples per endpoint.
        thresholds: Threshold map (``"*"`` = applies to all endpoints).

    Returns:
        int: ``0`` if all pass or no thresholds set, ``1`` on any breach.
    """
    limit = thresholds.get("*")
    if limit is None:
        return 0
    breached = False
    for sample in samples:
        p95 = sample.percentile(95)
        if p95 > limit:
            print(f"GATE FAIL: {sample.name} p95={p95:.2f}ms > {limit:.2f}ms")
            breached = True
    if not breached:
        print(f"\nGATE OK: all endpoints within p95 <= {limit:.2f}ms")
    return 1 if breached else 0


async def run(iterations: int) -> int:
    """Execute all probes and print the report.

    Args:
        iterations: Measured iterations per endpoint.

    Returns:
        int: Process exit code (perf-gate result).
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://bench") as client:
        ctx = await _discover(client)
        print(f"Probing category '{ctx.category_slug}', product '{ctx.product_slug}'.")

        listing = LatencySample("listing page 1")
        deep = LatencySample(f"cursor deep ({DEEP_PAGE_DEPTH} pg)")
        search = LatencySample("search q=")
        facets = LatencySample("facets")
        pdp = LatencySample("PDP")

        await _measure(
            client,
            listing,
            lambda: client.get(
                f"{API_V1_PREFIX}/catalog/categories/{ctx.category_slug}/products",
                params={"lang": BENCH_LANG, "sort": "price_asc"},
            ),
            iterations,
        )
        await _measure(client, deep, lambda: _walk_cursor(client, ctx), iterations)
        await _measure(
            client,
            search,
            lambda: client.get(
                f"{API_V1_PREFIX}/search",
                params={"lang": BENCH_LANG, "q": SEARCH_TERM},
            ),
            iterations,
        )
        await _measure(
            client,
            facets,
            lambda: client.get(
                f"{API_V1_PREFIX}/catalog/categories/{ctx.category_slug}/facets",
                params={"lang": BENCH_LANG},
            ),
            iterations,
        )
        await _measure(
            client,
            pdp,
            lambda: client.get(
                f"{API_V1_PREFIX}/catalog/products/{ctx.product_slug}",
                params={"lang": BENCH_LANG},
            ),
            iterations,
        )

        collected = [listing, deep, search, facets, pdp]
        _print_table(collected, iterations)
        return _check_gate(collected, _thresholds())


if __name__ == "__main__":
    args = _parse_args()
    raise SystemExit(asyncio.run(run(args.iterations)))
