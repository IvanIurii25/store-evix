"""Import the flystore.md catalog (products + categories + images) via the admin API.

Reads the scraped dataset (``media-content/``: ``categories.json`` and
``products/<slug>/{product.json,images/*}``) and creates categories, products
and product media through ``/api/v1/admin/*``.

Source data is Russian-only; to satisfy the both-languages publication rule the
Russian text is written to BOTH ``ru`` and ``ro`` (real ``ro`` translation is a
follow-on). Prices are in minor units (``"29900"`` → ``299.00`` MDL).

Env:
    MEDIA_DIR       path to media-content (required)
    API_BASE        default https://shop.evix.md
    API_HOST_IP     optional: pin API_BASE host to this IPv4 (edge/DNS workaround)
    ADMIN_EMAIL / ADMIN_PASSWORD   admin staff creds
    LIMIT           max products to import (0 = all; default 12 for a pilot)

Usage: uv run python scripts/import_flystore.py
"""

from __future__ import annotations

import html
import json
import os
import re
import socket
import sys
import time
from decimal import Decimal
from pathlib import Path

import httpx

MEDIA_DIR = Path(os.environ["MEDIA_DIR"])
API_BASE = os.environ.get("API_BASE", "https://shop.evix.md").rstrip("/")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@evix.md")
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
LIMIT = int(os.environ.get("LIMIT", "12"))
_HOST_IP = os.environ.get("API_HOST_IP")

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _pin_dns() -> None:
    """Pin the API hostname to a fixed IPv4 (system resolver may lack the A record)."""
    if not _HOST_IP:
        return
    host = API_BASE.split("://", 1)[-1].split("/", 1)[0]
    orig = socket.getaddrinfo

    def patched(h, port, *a, **k):  # noqa: ANN001, ANN202
        if h == host:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_HOST_IP, port))]
        return orig(h, port, *a, **k)

    socket.getaddrinfo = patched


def _slugify(value: str, fallback: str) -> str:
    s = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return s if len(s) >= 2 else fallback


def _plain(html_text: str) -> str:
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", html_text or ""))).strip()


def _price(minor: str | None) -> str | None:
    if not minor:
        return None
    return str((Decimal(minor) / 100).quantize(Decimal("0.01")))


class Api:
    def __init__(self) -> None:
        self.c = httpx.Client(base_url=API_BASE, timeout=60)
        r = self.c.post(
            "/api/v1/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        r.raise_for_status()
        self.c.headers["Authorization"] = f"Bearer {r.json()['access']}"

    def create_category(self, name: str, slug: str) -> int:
        body = {
            "is_active": True,
            "translations": [
                {"lang": lang, "name": name, "slug": slug} for lang in ("ru", "ro")
            ],
        }
        r = self.c.post("/api/v1/admin/categories", json=body)
        r.raise_for_status()
        return r.json()["id"]

    def category_id(self, slug: str) -> int | None:
        """Resolve an existing category's id by slug (idempotent re-runs)."""
        r = self.c.get(f"/api/v1/catalog/categories/{slug}", params={"lang": "ru"})
        return r.json()["id"] if r.status_code == 200 else None

    def create_product(self, payload: dict) -> int:
        r = self.c.post("/api/v1/admin/products", json=payload)
        r.raise_for_status()
        return r.json()["id"]

    def upload_media(self, product_id: int, path: Path) -> None:
        with path.open("rb") as fh:
            r = self.c.post(
                f"/api/v1/admin/products/{product_id}/media",
                files={"file": (path.name, fh, "image/png")},
            )
        r.raise_for_status()

    def activate(self, product_id: int) -> None:
        r = self.c.patch(
            f"/api/v1/admin/products/{product_id}", json={"is_active": True}
        )
        r.raise_for_status()


def _first_image(product_dir: Path) -> Path | None:
    images = product_dir / "images"
    if not images.is_dir():
        return None
    for f in sorted(images.iterdir()):
        if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            return f
    return None


def main() -> int:
    _pin_dns()
    api = Api()

    # Categories: create each source category, map slug -> store id.
    cats = json.loads((MEDIA_DIR / "categories.json").read_text())
    cat_id: dict[str, int] = {}
    for c in cats:
        slug = _slugify(c["slug"], f"cat-{c['id']}")
        try:
            cat_id[c["slug"]] = api.create_category(c["name"], slug)
        except httpx.HTTPStatusError:
            existing = api.category_id(slug)  # already created (re-run) -> reuse
            if existing:
                cat_id[c["slug"]] = existing
    print(f"categories: {len(cat_id)}/{len(cats)}")

    product_dirs = sorted((MEDIA_DIR / "products").iterdir())
    done = 0
    for pdir in product_dirs:
        if LIMIT and done >= LIMIT:
            break
        pj = pdir / "product.json"
        if not pj.is_file():
            continue
        p = json.loads(pj.read_text())
        img = _first_image(pdir)
        cslug = (p.get("category_slugs") or [None])[0]
        if img is None or cslug not in cat_id:
            continue  # need an image + a known category to be useful

        name = p["name"]
        slug = _slugify(p["slug"], f"p-{p['id']}")
        desc = _plain(p.get("description", ""))[:4000]
        tr = [
            {"lang": lang, "name": name, "slug": slug, "description": desc}
            for lang in ("ru", "ro")
        ]
        payload = {
            "category_id": cat_id[cslug],
            "code": str(p.get("sku") or f"P{p['id']}")[:128],
            "price": _price(p.get("prices", {}).get("price")) or "0",
            "qty": 10 if p.get("stock", {}).get("is_in_stock") else 0,
            "is_active": False,
            "translations": tr,
        }
        old = _price(p.get("prices", {}).get("regular_price"))
        if p.get("on_sale") and old and old != payload["price"]:
            payload["old_price"] = old

        try:
            pid = api.create_product(payload)
            api.upload_media(pid, img)
            api.activate(pid)
            done += 1
            print(f"  ✓ {slug} (id={pid}, {payload['price']} MDL)")
        except httpx.HTTPStatusError as e:
            print(f"  ! {slug}: {e.response.status_code} {e.response.text[:120]}")
        time.sleep(0.15)

    print(f"\nimported {done} products")
    return 0


if __name__ == "__main__":
    sys.exit(main())
