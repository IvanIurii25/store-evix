"""Router-glue API tests for the catalog admin-API — products (B7, §10).

Drives the product endpoint handlers through the ``client`` fixture to cover
create / get / update / delete / translation / attribute-link handlers, the
restock overview endpoints, and the media reorder/delete branches — with their
domain-error → HTTP-envelope mappings (404). The service layer itself is
covered by the ``test_admin_catalog_service_*`` files; happy paths already in
``test_admin_catalog_api.py`` / ``test_admin_media_and_filters.py`` are not
duplicated here.
"""

from io import BytesIO

import pytest
from httpx import AsyncClient

from tests.admin_catalog._router_helpers import (
    ADMIN,
    HTTP_CREATED,
    HTTP_NO_CONTENT,
    HTTP_NOT_FOUND,
    HTTP_OK,
    MISSING_ID,
    make_category,
    make_product,
    png_bytes,
    product_payload,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Products: create / get / update / delete / translation / attributes
# --------------------------------------------------------------------------- #
class TestProductLifecycleHandlers:
    """Product create/get/update/delete + translation + attribute handlers."""

    async def test_create_missing_category_maps_to_404(
        self, client: AsyncClient
    ) -> None:
        # Act: create a product referencing a non-existent category.
        resp = await client.post(
            f"{ADMIN}/products", json=product_payload(MISSING_ID, "SKU-NC", "nc")
        )

        # Assert: AdminNotFoundError from _get_category → not_found.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_get_missing_product_maps_to_404(self, client: AsyncClient) -> None:
        # Act: GET a product that does not exist.
        resp = await client.get(f"{ADMIN}/products/{MISSING_ID}")

        # Assert: AdminNotFoundError → not_found.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_update_missing_product_maps_to_404(
        self, client: AsyncClient
    ) -> None:
        # Act: PATCH a non-existent product.
        resp = await client.patch(f"{ADMIN}/products/{MISSING_ID}", json={"qty": 3})

        # Assert: AdminNotFoundError → not_found.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_update_price_success_echoes_new_value(
        self, client: AsyncClient
    ) -> None:
        # Arrange: an inactive product.
        category_id = await make_category(client)
        product_id = await make_product(client, category_id, "SKU-UPD")

        # Act: patch a structural field (no activation).
        resp = await client.patch(
            f"{ADMIN}/products/{product_id}", json={"price": "12.50"}
        )

        # Assert: the rebuilt ProductOut echoes the new price.
        assert resp.status_code == HTTP_OK, resp.text
        assert resp.json()["price"] == "12.50"

    async def test_delete_product_returns_204(self, client: AsyncClient) -> None:
        # Arrange: a product to delete.
        category_id = await make_category(client)
        product_id = await make_product(client, category_id, "SKU-RM")

        # Act: delete it.
        resp = await client.delete(f"{ADMIN}/products/{product_id}")

        # Assert: 204 and the product is gone.
        assert resp.status_code == HTTP_NO_CONTENT, resp.text
        gone = await client.get(f"{ADMIN}/products/{product_id}")
        assert gone.status_code == HTTP_NOT_FOUND

    async def test_delete_missing_product_maps_to_404(
        self, client: AsyncClient
    ) -> None:
        # Act: delete a product that does not exist.
        resp = await client.delete(f"{ADMIN}/products/{MISSING_ID}")

        # Assert: AdminNotFoundError → not_found.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_set_translation_success_returns_it(
        self, client: AsyncClient
    ) -> None:
        # Arrange: an existing product.
        category_id = await make_category(client)
        product_id = await make_product(client, category_id, "SKU-TR")

        # Act: upsert the ru translation.
        resp = await client.put(
            f"{ADMIN}/products/{product_id}/translations",
            json={"lang": "ru", "name": "New Name", "slug": "new-name-ru"},
        )

        # Assert: the upserted translation is returned.
        assert resp.status_code == HTTP_OK, resp.text
        assert resp.json()["slug"] == "new-name-ru"

    async def test_set_translation_missing_product_maps_to_404(
        self, client: AsyncClient
    ) -> None:
        # Act: upsert a translation on a non-existent product.
        resp = await client.put(
            f"{ADMIN}/products/{MISSING_ID}/translations",
            json={"lang": "ru", "name": "X", "slug": "x-slug"},
        )

        # Assert: AdminNotFoundError → not_found.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_set_attributes_missing_product_maps_to_404(
        self, client: AsyncClient
    ) -> None:
        # Act: set attribute links on a product that does not exist.
        resp = await client.put(
            f"{ADMIN}/products/{MISSING_ID}/attributes", json={"value_ids": []}
        )

        # Assert: AdminNotFoundError → not_found.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_set_attributes_empty_clears_links(self, client: AsyncClient) -> None:
        # Arrange: an existing product (no links yet).
        category_id = await make_category(client)
        product_id = await make_product(client, category_id, "SKU-AT")

        # Act: set an empty link set (valid — clears links).
        resp = await client.put(
            f"{ADMIN}/products/{product_id}/attributes", json={"value_ids": []}
        )

        # Assert: the rebuilt ProductOut has no value ids.
        assert resp.status_code == HTTP_OK, resp.text
        assert resp.json()["value_ids"] == []


# --------------------------------------------------------------------------- #
# Products: restock overview + media reorder/delete
# --------------------------------------------------------------------------- #
class TestProductRestockAndMediaHandlers:
    """Restock overview endpoints + media reorder/delete branches."""

    async def test_restock_waiters_returns_zero_for_no_subscribers(
        self, client: AsyncClient
    ) -> None:
        # Arrange: a product with no restock subscriptions.
        category_id = await make_category(client)
        product_id = await make_product(client, category_id, "SKU-WAIT")

        # Act: query the waiter count.
        resp = await client.get(f"{ADMIN}/products/{product_id}/restock-waiters")

        # Assert: the handler returns a zero count.
        assert resp.status_code == HTTP_OK, resp.text
        assert resp.json()["count"] == 0

    async def test_restock_demand_overview_returns_a_list(
        self, client: AsyncClient
    ) -> None:
        # Act: fetch the (empty) demand overview.
        resp = await client.get(f"{ADMIN}/restock/demand", params={"lang": "ro"})

        # Assert: the handler returns a JSON list.
        assert resp.status_code == HTTP_OK, resp.text
        assert isinstance(resp.json(), list)

    async def test_reorder_media_missing_product_maps_to_404(
        self, client: AsyncClient
    ) -> None:
        # Act: reorder media for a product that does not exist.
        resp = await client.put(
            f"{ADMIN}/products/{MISSING_ID}/media/reorder",
            json={"ordered_ids": [1]},
        )

        # Assert: AdminNotFoundError → not_found.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_delete_media_success_returns_204(
        self, client: AsyncClient, tmp_path, monkeypatch
    ) -> None:
        # Arrange: local storage + a product with one uploaded image.
        from app.core import config as cfg

        monkeypatch.setattr(cfg.settings, "storage_backend", "local")
        monkeypatch.setattr(cfg.settings, "media_root", str(tmp_path))
        monkeypatch.setattr(cfg.settings, "media_url_prefix", "/media")
        category_id = await make_category(client)
        product_id = await make_product(client, category_id, "SKU-DELMED")
        upload = await client.post(
            f"{ADMIN}/products/{product_id}/media",
            files={"file": ("p.png", BytesIO(png_bytes()), "image/png")},
        )
        assert upload.status_code == HTTP_CREATED, upload.text
        media_id = upload.json()["id"]

        # Act: delete the media via the owning product path.
        resp = await client.delete(f"{ADMIN}/products/{product_id}/media/{media_id}")

        # Assert: no-content success.
        assert resp.status_code == HTTP_NO_CONTENT, resp.text
