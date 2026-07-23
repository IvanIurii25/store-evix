"""Shared payload builders + API helpers for the admin-catalog router tests.

Not a test module (leading underscore → pytest does not collect it). The
router-focused test files (``test_admin_catalog_router*.py``) import these to
build create payloads and seed rows through the API without duplicating the
same boilerplate in each file.
"""

from io import BytesIO

from httpx import AsyncClient
from PIL import Image

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
ADMIN: str = "/api/v1/admin"
MISSING_ID: int = 999999
HTTP_OK: int = 200
HTTP_CREATED: int = 201
HTTP_NO_CONTENT: int = 204
HTTP_BAD_REQUEST: int = 400
HTTP_NOT_FOUND: int = 404
HTTP_CONFLICT: int = 409
HTTP_UNPROCESSABLE: int = 422


# --------------------------------------------------------------------------- #
# Payload builders
# --------------------------------------------------------------------------- #
def png_bytes(width: int = 1000, height: int = 800) -> bytes:
    """Return an in-memory, genuinely decodable PNG (the upload gate needs one)."""
    buffer = BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format="PNG")
    return buffer.getvalue()


def cat_payload(name: str, slug: str, parent_id: int | None = None) -> dict:
    """Build a two-language category create payload."""
    return {
        "parent_id": parent_id,
        "is_active": False,
        "translations": [
            {"lang": "ru", "name": f"{name}-ru", "slug": f"{slug}-ru"},
            {"lang": "ro", "name": f"{name}-ro", "slug": f"{slug}-ro"},
        ],
    }


def product_payload(category_id: int, code: str, slug: str) -> dict:
    """Build a two-language product create payload (inactive by default)."""
    return {
        "category_id": category_id,
        "code": code,
        "price": "10.00",
        "qty": 5,
        "is_active": False,
        "translations": [
            {"lang": "ru", "name": f"{slug}-ru", "slug": f"{slug}-ru"},
            {"lang": "ro", "name": f"{slug}-ro", "slug": f"{slug}-ro"},
        ],
    }


# --------------------------------------------------------------------------- #
# API seed helpers
# --------------------------------------------------------------------------- #
async def make_category(
    client: AsyncClient, name: str = "Cat", slug: str = "cat"
) -> int:
    """Create a category via the API and return its id."""
    resp = await client.post(f"{ADMIN}/categories", json=cat_payload(name, slug))
    assert resp.status_code == HTTP_CREATED, resp.text
    return resp.json()["id"]


async def make_product(client: AsyncClient, category_id: int, code: str) -> int:
    """Create a product via the API and return its id."""
    resp = await client.post(
        f"{ADMIN}/products", json=product_payload(category_id, code, code.lower())
    )
    assert resp.status_code == HTTP_CREATED, resp.text
    return resp.json()["id"]


async def make_attribute(client: AsyncClient, code: str = "attr") -> int:
    """Create an attribute via the API and return its id."""
    resp = await client.post(
        f"{ADMIN}/attributes",
        json={
            "code": code,
            "translations": [
                {"lang": "ru", "name": f"{code}-ru"},
                {"lang": "ro", "name": f"{code}-ro"},
            ],
        },
    )
    assert resp.status_code == HTTP_CREATED, resp.text
    return resp.json()["id"]


async def make_value(client: AsyncClient, attribute_id: int) -> int:
    """Create a value under an attribute via the API and return its id."""
    resp = await client.post(
        f"{ADMIN}/attributes/{attribute_id}/values",
        json={
            "translations": [
                {"lang": "ru", "value": "val-ru"},
                {"lang": "ro", "value": "val-ro"},
            ]
        },
    )
    assert resp.status_code == HTTP_CREATED, resp.text
    return resp.json()["id"]
