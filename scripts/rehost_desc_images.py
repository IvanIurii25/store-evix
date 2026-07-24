"""Rehost description-image banners from flystore.md onto our own storage.

For every imported product, uploads its local full-res description images
(``products/<slug>/description-images/``) to ``POST /admin/assets`` (MinIO,
served at media.evix.md), rewrites the ``<img src>`` URLs in the product
description from the flystore.md originals to the rehosted URLs, and saves the
new description back via ``PUT /admin/products/{id}/translations`` (ru + ro).

Idempotent/resumable: a product whose stored description no longer references
``flystore.md`` is skipped. Uploads are de-duplicated by source URL. Missing
local files (e.g. the handful of external-domain banners that never downloaded)
are left as-is.

Env:
    MEDIA_DIR       path to media-content (required)
    API_BASE        default https://shop.evix.md
    API_HOST_IP     optional IPv4 pin
    ADMIN_EMAIL / ADMIN_PASSWORD   admin creds
    LIMIT           max products to process (0 = all)

Usage: uv run python scripts/rehost_desc_images.py
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
from pathlib import Path

import httpx

MEDIA_DIR = Path(os.environ["MEDIA_DIR"])
API_BASE = os.environ.get("API_BASE", "https://shop.evix.md").rstrip("/")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@evix.md")
LIMIT = int(os.environ.get("LIMIT", "0"))
_HOST_IP = os.environ.get("API_HOST_IP")

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
}


def _pin_dns() -> None:
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


class Api:
    def __init__(self) -> None:
        self.c = httpx.Client(base_url=API_BASE, timeout=120)
        r = self.c.post(
            "/api/v1/auth/login",
            json={"email": ADMIN_EMAIL, "password": os.environ["ADMIN_PASSWORD"]},
        )
        r.raise_for_status()
        self.c.headers["Authorization"] = f"Bearer {r.json()['access']}"

    @staticmethod
    def _retry(fn, tries: int = 4, base: float = 1.5):
        """Call ``fn`` with retry+backoff on transient transport errors."""
        for i in range(tries):
            try:
                return fn()
            except httpx.TransportError:
                if i == tries - 1:
                    raise
                time.sleep(base * (2**i))

    def get_product(self, slug: str) -> dict | None:
        def _do():
            return self.c.get(f"/api/v1/catalog/products/{slug}", params={"lang": "ru"})

        r = self._retry(_do)
        return r.json() if r.status_code == 200 else None

    def upload_asset(self, path: Path) -> str:
        mime = _MIME.get(path.suffix.lower(), "application/octet-stream")

        def _do():
            with path.open("rb") as fh:
                resp = self.c.post(
                    "/api/v1/admin/assets", files={"file": (path.name, fh, mime)}
                )
            resp.raise_for_status()
            return resp

        return self._retry(_do).json()["url"]

    def set_translation(
        self, pid: int, lang: str, name: str, slug: str, desc: str
    ) -> None:
        def _do():
            resp = self.c.put(
                f"/api/v1/admin/products/{pid}/translations",
                json={"lang": lang, "name": name, "slug": slug, "description": desc},
            )
            resp.raise_for_status()
            return resp

        self._retry(_do)


def main() -> int:
    _pin_dns()
    api = Api()
    cache: dict[str, str] = {}  # source url -> rehosted url (dedup uploads)

    product_dirs = sorted((MEDIA_DIR / "products").iterdir())
    done = skipped = failed = uploads = 0
    for pdir in product_dirs:
        if LIMIT and done >= LIMIT:
            break
        pj = pdir / "product.json"
        if not pj.is_file():
            continue
        p = json.loads(pj.read_text())
        locals_ = p.get("description_images_local") or []
        if not locals_:
            continue

        slug = _slugify(p["slug"], f"p-{p['id']}")
        detail = api.get_product(slug)
        if detail is None:
            skipped += 1
            continue
        # Un-rehosted image srcs (absolute flystore.md OR relative "/wp-content/…")
        # all contain "wp-content"; rehosted media.evix.md URLs do not.
        if "wp-content" not in (detail.get("description") or ""):
            skipped += 1  # already fully rehosted
            continue

        desc = p.get("description") or ""
        try:
            for entry in locals_:
                src = entry["url"]
                fpath = pdir / "description-images" / entry["file"]
                if not fpath.is_file():
                    continue  # never downloaded (external domain) — leave as-is
                if src not in cache:
                    cache[src] = api.upload_asset(fpath)
                    uploads += 1
                desc = desc.replace(src, cache[src])

            api.set_translation(detail["id"], "ru", p["name"], slug, desc)
            api.set_translation(detail["id"], "ro", p["name"], slug, desc)
            done += 1
            if done % 25 == 0:
                print(
                    f"[{done}] rehosted (uploads={uploads}, skipped={skipped})",
                    flush=True,
                )
        except httpx.HTTPStatusError as e:
            failed += 1
            print(
                f"  ! {slug}: {e.response.status_code} {e.response.text[:120]}",
                flush=True,
            )
        except httpx.HTTPError as e:
            failed += 1
            print(f"  ! {slug}: transport {type(e).__name__}", flush=True)
        time.sleep(0.05)

    print(
        f"\ndone={done} skipped={skipped} failed={failed} uploads={uploads}", flush=True
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
