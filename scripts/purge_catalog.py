"""Purge the evix-store catalog (products + categories) via the admin API.

Intended as a one-shot reset before a clean full re-import (e.g. to drop an
earlier pilot import whose categories were created flat, without hierarchy).
Deletes ALL products, then ALL categories (leaf-first). Attributes are left
untouched.

DESTRUCTIVE. Requires ``CONFIRM=1`` to actually delete; otherwise it only
reports what it would remove.

Env:
    API_BASE        default https://shop.evix.md
    API_HOST_IP     optional: pin API_BASE host to this IPv4
    ADMIN_EMAIL / ADMIN_PASSWORD   admin staff creds
    CONFIRM         "1" → perform deletions (else dry report)

Usage: CONFIRM=1 uv run python scripts/purge_catalog.py
"""

from __future__ import annotations

import os
import socket
import sys
import time

import httpx

API_BASE = os.environ.get("API_BASE", "https://shop.evix.md").rstrip("/")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@evix.md")
CONFIRM = os.environ.get("CONFIRM") == "1"
_HOST_IP = os.environ.get("API_HOST_IP")


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


def _login() -> httpx.Client:
    c = httpx.Client(base_url=API_BASE, timeout=60)
    r = c.post(
        "/api/v1/auth/login",
        json={"email": ADMIN_EMAIL, "password": os.environ["ADMIN_PASSWORD"]},
    )
    r.raise_for_status()
    c.headers["Authorization"] = f"Bearer {r.json()['access']}"
    return c


def purge_products(c: httpx.Client) -> int:
    removed = 0
    while True:
        r = c.get("/api/v1/admin/products", params={"limit": 200})
        r.raise_for_status()
        items = r.json().get("data", [])
        if not items:
            break
        if not CONFIRM:
            return len(items)  # report the first page count only in dry mode
        for it in items:
            d = c.delete(f"/api/v1/admin/products/{it['id']}")
            d.raise_for_status()
            removed += 1
            time.sleep(0.05)
        print(f"  products removed: {removed}", flush=True)
    return removed


def purge_categories(c: httpx.Client) -> int:
    r = c.get("/api/v1/admin/categories")
    r.raise_for_status()
    cats = r.json()
    if not CONFIRM:
        return len(cats)
    removed = 0
    # Leaf-first: deepest categories deleted before their parents.
    for cat in sorted(cats, key=lambda x: x["depth"], reverse=True):
        d = c.delete(f"/api/v1/admin/categories/{cat['id']}")
        d.raise_for_status()
        removed += 1
        time.sleep(0.05)
    print(f"  categories removed: {removed}", flush=True)
    return removed


def main() -> int:
    _pin_dns()
    c = _login()
    if not CONFIRM:
        p = purge_products(c)
        cats = purge_categories(c)
        print(f"[DRY] would delete ~{p} products (first page) + {cats} categories. "
              f"Set CONFIRM=1 to execute.")
        return 0
    prods = purge_products(c)
    cats = purge_categories(c)
    print(f"\npurged: {prods} products, {cats} categories")
    return 0


if __name__ == "__main__":
    sys.exit(main())
