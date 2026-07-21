"""Import the flystore.md catalog (categories + products + images + attributes)
into evix-store via the admin API.

Reads the scraped dataset (``media-content/``: ``categories.json`` and
``products/<slug>/{product.json,images/*}``) produced by the PersonalAssistant
scraper, and creates the full catalog through ``/api/v1/admin/*``.

Mapping decisions (2026-07-21):

* **Categories** — created in topological order (parents before children) so the
  ``parent_id`` hierarchy is preserved.
* **Description** — imported as the **full HTML** from the source (embedded
  banner ``<img>`` still hotlink flystore.md; rehosting is a follow-on).
* **Variable products** (144) — imported as a **single product at the parent
  price**; their options are carried as product attributes (the schema has no
  variant model).
* **Attributes** — the small source dictionary (Цвет/Тип/Размер/Материал) is
  created as ``attribute`` + ``attribute_value`` rows and linked per product,
  enabling the storefront facet filters.
* **Images** — the **whole gallery** is uploaded (``images/NN_*``), in order.
  Variation images (``var_*``) and ``index.json`` are skipped.

Source text is Russian-only; to satisfy the both-languages publication rule the
Russian text is written to BOTH ``ru`` and ``ro`` (real ``ro`` is a follow-on).
Prices are in minor units (``"29900"`` → ``299.00`` MDL).

Idempotent: categories/attributes are resolved by slug/text on re-run; a product
whose ``ru`` slug already exists is skipped. Nothing is deleted.

Env:
    MEDIA_DIR       path to media-content (required)
    API_BASE        default https://shop.evix.md
    API_HOST_IP     optional: pin API_BASE host to this IPv4 (edge/DNS workaround)
    ADMIN_EMAIL / ADMIN_PASSWORD   admin staff creds (password required unless DRY_RUN)
    LIMIT           max products to import (0 = all; default 0)
    DRY_RUN         "1" → build payloads and print stats, no API calls

Usage: uv run python scripts/import_flystore.py
"""

from __future__ import annotations

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
LIMIT = int(os.environ.get("LIMIT", "0"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
_HOST_IP = os.environ.get("API_HOST_IP")

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}
_UNCATEGORIZED = "uncategorized"


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


def _price(minor: str | None) -> str | None:
    if not minor:
        return None
    return str((Decimal(minor) / 100).quantize(Decimal("0.01")))


def _qty(p: dict) -> int:
    stock = p.get("stock") or {}
    if not stock.get("is_in_stock"):
        return 0
    low = stock.get("low_stock_remaining")
    return int(low) if isinstance(low, int) and low > 0 else 100


def _gallery(product_dir: Path) -> list[Path]:
    """All gallery image files for a product, ordered; excludes variation images."""
    d = product_dir / "images"
    if not d.is_dir():
        return []
    return [
        f
        for f in sorted(d.iterdir())
        if f.suffix.lower() in _IMG_EXT and not f.name.startswith("var_")
    ]


class Api:
    """Thin admin-API client. In DRY_RUN mode returns synthetic ids, no network."""

    def __init__(self) -> None:
        self._id = 0
        if DRY_RUN:
            self.c = None
            return
        self.c = httpx.Client(base_url=API_BASE, timeout=60)
        r = self.c.post(
            "/api/v1/auth/login",
            json={"email": ADMIN_EMAIL, "password": os.environ["ADMIN_PASSWORD"]},
        )
        r.raise_for_status()
        self.c.headers["Authorization"] = f"Bearer {r.json()['access']}"

    def _next(self) -> int:
        self._id += 1
        return self._id

    # -- categories --
    def create_category(self, name: str, slug: str, parent_id: int | None) -> int:
        if DRY_RUN:
            return self._next()
        body = {
            "parent_id": parent_id,
            "is_active": True,
            "translations": [
                {"lang": lang, "name": name, "slug": slug} for lang in ("ru", "ro")
            ],
        }
        r = self.c.post("/api/v1/admin/categories", json=body)
        r.raise_for_status()
        return r.json()["id"]

    def list_categories(self) -> list[dict]:
        """All categories (active or not) with translations — for idempotent resolve."""
        if DRY_RUN:
            return []
        r = self.c.get("/api/v1/admin/categories")
        r.raise_for_status()
        return r.json()

    def activate_category(self, category_id: int) -> None:
        if DRY_RUN:
            return
        r = self.c.patch(
            f"/api/v1/admin/categories/{category_id}", json={"is_active": True}
        )
        r.raise_for_status()

    # -- attributes --
    def list_attributes(self) -> list[dict]:
        """Existing attributes for registry preload; [] if the API has no list GET."""
        if DRY_RUN:
            return []
        r = self.c.get("/api/v1/admin/attributes")
        if r.status_code == 405:  # no list endpoint — start empty, create lazily
            return []
        r.raise_for_status()
        return r.json()

    def create_attribute(self, code: str, name: str) -> int:
        if DRY_RUN:
            return self._next()
        body = {
            "code": code,
            "translations": [{"lang": lang, "name": name} for lang in ("ru", "ro")],
        }
        r = self.c.post("/api/v1/admin/attributes", json=body)
        r.raise_for_status()
        return r.json()["id"]

    def create_value(self, attribute_id: int, value: str) -> int:
        if DRY_RUN:
            return self._next()
        body = {"translations": [{"lang": lang, "value": value} for lang in ("ru", "ro")]}
        r = self.c.post(f"/api/v1/admin/attributes/{attribute_id}/values", json=body)
        r.raise_for_status()
        return r.json()["id"]

    def set_product_attributes(self, product_id: int, value_ids: list[int]) -> None:
        if DRY_RUN:
            return
        r = self.c.put(
            f"/api/v1/admin/products/{product_id}/attributes",
            json={"value_ids": value_ids},
        )
        r.raise_for_status()

    # -- products --
    def product_exists(self, slug: str) -> bool:
        if DRY_RUN:
            return False
        r = self.c.get(f"/api/v1/catalog/products/{slug}", params={"lang": "ru"})
        return r.status_code == 200

    def create_product(self, payload: dict) -> int:
        if DRY_RUN:
            return self._next()
        r = self.c.post("/api/v1/admin/products", json=payload)
        r.raise_for_status()
        return r.json()["id"]

    def upload_media(self, product_id: int, path: Path) -> None:
        if DRY_RUN:
            return
        mime = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
        with path.open("rb") as fh:
            r = self.c.post(
                f"/api/v1/admin/products/{product_id}/media",
                files={"file": (path.name, fh, mime)},
            )
        r.raise_for_status()

    def activate(self, product_id: int) -> None:
        if DRY_RUN:
            return
        r = self.c.patch(
            f"/api/v1/admin/products/{product_id}", json={"is_active": True}
        )
        r.raise_for_status()


class AttrRegistry:
    """Resolves source attributes/values to store ids, creating them on demand."""

    def __init__(self, api: Api) -> None:
        self.api = api
        self.attr: dict[str, int] = {}          # code -> attribute id
        self.val: dict[tuple[str, str], int] = {}  # (code, value_text) -> value id
        for a in api.list_attributes():
            code = a["code"]
            self.attr[code] = a["id"]
            for v in a.get("values", []):
                name = next(
                    (t["value"] for t in v.get("translations", []) if t["lang"] == "ru"),
                    None,
                )
                if name:
                    self.val[(code, name)] = v["id"]

    def attribute(self, code: str, name: str) -> int:
        if code not in self.attr:
            self.attr[code] = self.api.create_attribute(code, name)
        return self.attr[code]

    def value(self, code: str, attribute_id: int, text: str) -> int:
        key = (code, text)
        if key not in self.val:
            self.val[key] = self.api.create_value(attribute_id, text)
        return self.val[key]

    def value_ids_for(self, product: dict) -> list[int]:
        ids: list[int] = []
        for at in product.get("attributes") or []:
            name = at.get("name")
            if not name:
                continue
            code = at.get("taxonomy") or _slugify(name, "attr")
            aid = self.attribute(code, name)
            for term in at.get("terms") or []:
                text = term.get("name")
                if text:
                    ids.append(self.value(code, aid, text))
        return ids


def import_categories(api: Api) -> dict[str, int]:
    """Create categories parents-first (active); return source-slug -> store-id.

    Idempotent via the admin list: an existing category (matched by its ``ru``
    slug) is reused and activated if needed, rather than re-created.
    """
    cats = json.loads((MEDIA_DIR / "categories.json").read_text())
    by_slug = {c["slug"]: c for c in cats}

    # store_slug -> (id, is_active) from the admin listing (includes inactive).
    existing: dict[str, tuple[int, bool]] = {}
    for cat in api.list_categories():
        ru = next(
            (t["slug"] for t in cat.get("translations", []) if t["lang"] == "ru"), None
        )
        if ru:
            existing[ru] = (cat["id"], cat["is_active"])

    cat_id: dict[str, int] = {}

    def ensure(slug: str) -> int | None:
        if slug in cat_id:
            return cat_id[slug]
        c = by_slug.get(slug)
        if not c:
            return None
        parent_slug = c.get("parent_slug")
        parent_id = ensure(parent_slug) if parent_slug else None
        store_slug = _slugify(slug, f"cat-{c['id']}")
        if store_slug in existing:
            cid, is_active = existing[store_slug]
            if not is_active:
                api.activate_category(cid)
            cat_id[slug] = cid
        else:
            cat_id[slug] = api.create_category(c["name"], store_slug, parent_id)
        return cat_id[slug]

    for c in cats:
        ensure(c["slug"])
    print(f"categories: {len(cat_id)}/{len(cats)}")
    return cat_id


def build_product_payload(p: dict, category_id: int) -> dict:
    name = p["name"]
    slug = _slugify(p["slug"], f"p-{p['id']}")
    desc = p.get("description") or ""  # full HTML
    tr = [
        {"lang": lang, "name": name, "slug": slug, "description": desc}
        for lang in ("ru", "ro")
    ]
    price = _price((p.get("prices") or {}).get("price")) or "0"
    payload = {
        "category_id": category_id,
        "code": str(p.get("sku") or f"P{p['id']}")[:128],
        "price": price,
        "qty": _qty(p),
        "is_active": False,
        "translations": tr,
    }
    old = _price((p.get("prices") or {}).get("regular_price"))
    if p.get("on_sale") and old and old != price:
        payload["old_price"] = old
    return payload


def main() -> int:
    _pin_dns()
    api = Api()
    cat_id = import_categories(api)
    reg = AttrRegistry(api)
    uncat = cat_id.get(_UNCATEGORIZED)

    product_dirs = sorted((MEDIA_DIR / "products").iterdir())
    done = skipped = failed = imgs = attrs = 0
    for pdir in product_dirs:
        if LIMIT and done >= LIMIT:
            break
        pj = pdir / "product.json"
        if not pj.is_file():
            continue
        p = json.loads(pj.read_text())
        slug = _slugify(p["slug"], f"p-{p['id']}")

        if api.product_exists(slug):
            skipped += 1
            continue

        cslug = (p.get("category_slugs") or [None])[0]
        category_id = cat_id.get(cslug, uncat)
        if category_id is None:
            failed += 1
            print(f"  ! {slug}: no category (and no '{_UNCATEGORIZED}')")
            continue

        payload = build_product_payload(p, category_id)
        try:
            pid = api.create_product(payload)
            for img in _gallery(pdir):
                api.upload_media(pid, img)
                imgs += 1
            value_ids = reg.value_ids_for(p)
            if value_ids:
                api.set_product_attributes(pid, value_ids)
                attrs += 1
            api.activate(pid)
            done += 1
            if done % 25 == 0 or DRY_RUN:
                print(f"  ✓ {slug} (id={pid}, {payload['price']} MDL, "
                      f"{len(_gallery(pdir))} img, {len(value_ids)} attr-val)")
        except httpx.HTTPStatusError as e:
            failed += 1
            print(f"  ! {slug}: {e.response.status_code} {e.response.text[:140]}")
        if not DRY_RUN:
            time.sleep(0.1)

    print(f"\ndone={done} skipped={skipped} failed={failed} "
          f"images={imgs} products_with_attrs={attrs}"
          f"{'  [DRY_RUN]' if DRY_RUN else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
