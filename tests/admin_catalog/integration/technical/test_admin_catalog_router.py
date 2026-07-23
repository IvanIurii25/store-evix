"""Router-glue API tests for the catalog admin-API — categories (B7, §10).

The service layer is already exercised directly by the ``test_admin_catalog_service_*``
files; this module drives the **router** end-to-end through the ``client``
fixture to cover the category endpoint handlers and their domain-error →
HTTP-envelope branches (update / delete / translation / reorder / move).
Product and attribute handlers live in the sibling
``test_admin_catalog_router_products.py`` /
``test_admin_catalog_router_attributes.py`` files; the ``_raise_http``
fall-through unit test lives in ``test_admin_catalog_raise_http.py``.

It deliberately does NOT duplicate the happy paths already asserted in
``test_admin_catalog_api.py`` / ``test_admin_media_and_filters.py``.
"""

import pytest
from httpx import AsyncClient

from tests.admin_catalog._router_helpers import (
    ADMIN,
    HTTP_CONFLICT,
    HTTP_NO_CONTENT,
    HTTP_NOT_FOUND,
    HTTP_OK,
    MISSING_ID,
    cat_payload,
    make_category,
    make_product,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Categories: update / delete
# --------------------------------------------------------------------------- #
class TestCategoryUpdateDelete:
    """Category structural update + delete handlers and their error branches."""

    async def test_update_position_success_echoes_new_value(
        self, client: AsyncClient
    ) -> None:
        # Arrange: a fresh category with default position 0.
        category_id = await make_category(client)

        # Act: PATCH a new structural field (no parent change).
        resp = await client.patch(
            f"{ADMIN}/categories/{category_id}", json={"position": 7}
        )

        # Assert: the handler rebuilds the CategoryOut with the new value.
        assert resp.status_code == HTTP_OK, resp.text
        assert resp.json()["position"] == 7, "PATCH must echo the updated position"

    async def test_create_with_missing_parent_maps_to_404(
        self, client: AsyncClient
    ) -> None:
        # Act: create a category whose parent_id does not exist.
        resp = await client.post(
            f"{ADMIN}/categories",
            json=cat_payload("Orphan", "orphan", parent_id=MISSING_ID),
        )

        # Assert: AdminNotFoundError from _resolve_path → not_found envelope.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_update_missing_category_maps_to_404(
        self, client: AsyncClient
    ) -> None:
        # Act: PATCH a category that does not exist.
        resp = await client.patch(
            f"{ADMIN}/categories/{MISSING_ID}", json={"position": 1}
        )

        # Assert: AdminNotFoundError → not_found envelope.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_delete_empty_category_returns_204(self, client: AsyncClient) -> None:
        # Arrange: a leaf category with no children / products.
        category_id = await make_category(client)

        # Act: delete it.
        resp = await client.delete(f"{ADMIN}/categories/{category_id}")

        # Assert: no-content success and the row is gone from the listing.
        assert resp.status_code == HTTP_NO_CONTENT, resp.text
        listing = (await client.get(f"{ADMIN}/categories")).json()
        assert category_id not in {row["id"] for row in listing}

    async def test_delete_missing_category_maps_to_404(
        self, client: AsyncClient
    ) -> None:
        # Act: delete a non-existent category.
        resp = await client.delete(f"{ADMIN}/categories/{MISSING_ID}")

        # Assert: AdminNotFoundError → not_found.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_delete_category_with_products_is_conflict(
        self, client: AsyncClient
    ) -> None:
        # Arrange: a category that still owns a product.
        category_id = await make_category(client)
        await make_product(client, category_id, "SKU-BLOCK")

        # Act: attempt to delete the non-empty category.
        resp = await client.delete(f"{ADMIN}/categories/{category_id}")

        # Assert: AdminConflictError → 409 conflict.
        assert resp.status_code == HTTP_CONFLICT, resp.text
        assert resp.json()["error"]["code"] == "conflict"


# --------------------------------------------------------------------------- #
# Categories: translation / reorder / move
# --------------------------------------------------------------------------- #
class TestCategoryTranslationReorderMove:
    """PUT translation, reorder and move handlers + their error branches."""

    async def test_set_translation_upserts_and_returns_it(
        self, client: AsyncClient
    ) -> None:
        # Arrange: an existing category.
        category_id = await make_category(client)

        # Act: upsert (overwrite) the ru translation.
        resp = await client.put(
            f"{ADMIN}/categories/{category_id}/translations",
            json={"lang": "ru", "name": "Renamed", "slug": "renamed-ru"},
        )

        # Assert: the handler returns the upserted translation.
        assert resp.status_code == HTTP_OK, resp.text
        body = resp.json()
        assert body["name"] == "Renamed"
        assert body["slug"] == "renamed-ru"

    async def test_set_translation_missing_category_maps_to_404(
        self, client: AsyncClient
    ) -> None:
        # Act: upsert a translation on a category that does not exist.
        resp = await client.put(
            f"{ADMIN}/categories/{MISSING_ID}/translations",
            json={"lang": "ru", "name": "X", "slug": "x-slug"},
        )

        # Assert: AdminNotFoundError → not_found.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_reorder_assigns_new_positions(self, client: AsyncClient) -> None:
        # Arrange: two sibling root categories.
        first = await make_category(client, "First", "first")
        second = await make_category(client, "Second", "second")

        # Act: reorder them with explicit positions.
        resp = await client.post(
            f"{ADMIN}/categories/reorder",
            json={
                "items": [
                    {"category_id": first, "position": 5},
                    {"category_id": second, "position": 2},
                ]
            },
        )

        # Assert: 204 and the new positions are persisted.
        assert resp.status_code == HTTP_NO_CONTENT, resp.text
        listing = {
            row["id"]: row["position"]
            for row in (await client.get(f"{ADMIN}/categories")).json()
        }
        assert listing[first] == 5
        assert listing[second] == 2

    async def test_reorder_missing_category_maps_to_404(
        self, client: AsyncClient
    ) -> None:
        # Act: reorder referencing a category id that does not exist.
        resp = await client.post(
            f"{ADMIN}/categories/reorder",
            json={"items": [{"category_id": MISSING_ID, "position": 1}]},
        )

        # Assert: AdminNotFoundError → not_found.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_move_missing_category_maps_to_404(self, client: AsyncClient) -> None:
        # Act: move a category that does not exist to root.
        resp = await client.post(
            f"{ADMIN}/categories/{MISSING_ID}/move", json={"parent_id": None}
        )

        # Assert: AdminNotFoundError → not_found.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"
