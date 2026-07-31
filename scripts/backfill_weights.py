"""Backfill ``product.weight_g`` with per-category defaults (Nova Post phase P0).

Carriers price a parcel by weight, and the imported catalogue has none: the
flystore scrape carries no weight field at all, so every product starts NULL. A
NULL is honest ("unknown") but useless at checkout — the delivery service would
fall back to a flat per-item default for the whole cart.

This fills the gap one level up: a default per **category** (dozens of values a
human can actually decide) instead of per product (hundreds nobody will fill).
Products whose weight was entered by hand are never touched.

Two modes:

* **report** (default, no writes) — per category: how many products, how many
  still missing a weight. Use it to decide the numbers for the map.
* **fill** (``CONFIRM=1``) — writes ``weight_g`` for products where it IS NULL,
  taking the value from the map by category id, else ``DEFAULT_WEIGHT_G``.

The map is JSON, ``{"<category_id>": <grams>}``, e.g. ``{"12": 1500, "31": 250}``.
Category ids come from the report. A category absent from the map falls back to
``DEFAULT_WEIGHT_G``; set it to ``0`` to skip such categories entirely instead.

Idempotent: re-running only ever touches rows that are still NULL, so manual
corrections survive, and a second pass after adding map entries fills only the
newly-mapped categories.

Usage (in the app container):
    docker exec -i evix-store-app-1 uv run python scripts/backfill_weights.py
    docker exec -i evix-store-app-1 env CONFIRM=1 WEIGHTS_JSON=/tmp/w.json \\
        DEFAULT_WEIGHT_G=500 uv run python scripts/backfill_weights.py
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models.catalog import Category, CategoryTranslation, Product

CONFIRM = os.environ.get("CONFIRM") == "1"
WEIGHTS_JSON = os.environ.get("WEIGHTS_JSON", "")
# 0 disables the fallback: unmapped categories are then left untouched.
DEFAULT_WEIGHT_G = int(os.environ.get("DEFAULT_WEIGHT_G", "0"))
REPORT_LANG = os.environ.get("REPORT_LANG", "ro")


def _load_map() -> dict[int, int]:
    """Return the ``{category_id: grams}`` map from ``WEIGHTS_JSON`` (or empty).

    Returns:
        dict[int, int]: Parsed map; empty when no path is configured.

    Raises:
        SystemExit: If the path is set but unreadable or not a flat JSON object
            of positive integers — a silent misparse would write wrong weights.
    """
    if not WEIGHTS_JSON:
        return {}
    try:
        raw = json.loads(Path(WEIGHTS_JSON).read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"WEIGHTS_JSON unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit("WEIGHTS_JSON must be an object {category_id: grams}")
    out: dict[int, int] = {}
    for key, value in raw.items():
        try:
            cid, grams = int(key), int(value)
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"WEIGHTS_JSON bad entry {key!r}: {value!r}") from exc
        if grams <= 0:
            raise SystemExit(f"WEIGHTS_JSON non-positive weight for {cid}: {grams}")
        out[cid] = grams
    return out


async def main() -> None:
    """Report missing weights, or fill them from the category map."""
    weights = _load_map()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with maker() as session:
            rows = (
                await session.execute(
                    select(
                        Category.id,
                        CategoryTranslation.name,
                        func.count(Product.id),
                        func.count(Product.id).filter(Product.weight_g.is_(None)),
                    )
                    .select_from(Category)
                    .outerjoin(
                        CategoryTranslation,
                        (CategoryTranslation.category_id == Category.id)
                        & (CategoryTranslation.lang == REPORT_LANG),
                    )
                    .outerjoin(Product, Product.category_id == Category.id)
                    .group_by(Category.id, CategoryTranslation.name)
                    .order_by(func.count(Product.id).desc())
                )
            ).all()

            print(f"{'id':>5}  {'products':>8}  {'missing':>7}  {'planned':>7}  name")
            total, missing_total, planned_total = 0, 0, 0
            for cid, name, count, missing in rows:
                grams = weights.get(cid, DEFAULT_WEIGHT_G)
                planned = missing if grams > 0 else 0
                total += count
                missing_total += missing
                planned_total += planned
                mark = f"{grams}g" if planned else "-"
                print(f"{cid:>5}  {count:>8}  {missing:>7}  {mark:>7}  {name or '—'}")
            print(
                f"\ntotals: {total} products, {missing_total} without weight, "
                f"{planned_total} would be filled by this map"
            )

            if not CONFIRM:
                print("\nDRY RUN — no writes. Set CONFIRM=1 to apply.")
                return
            if not planned_total:
                print("\nNothing to fill (empty map and DEFAULT_WEIGHT_G=0).")
                return

            filled = 0
            for cid, _name, _count, missing in rows:
                grams = weights.get(cid, DEFAULT_WEIGHT_G)
                if not missing or grams <= 0:
                    continue
                result = await session.execute(
                    update(Product)
                    .where(Product.category_id == cid, Product.weight_g.is_(None))
                    .values(weight_g=grams)
                )
                filled += result.rowcount or 0
            await session.commit()
            print(f"\nfilled weight_g on {filled} products.")
            # No card rebuild / ES re-index: weight is not projected onto the
            # read-model and is not searchable — it only feeds delivery pricing.
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
