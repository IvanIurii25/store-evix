"""Router-glue API tests for the catalog admin-API — attributes (B7, §10).

Drives the attribute + attribute-value endpoint handlers through the ``client``
fixture to cover update / delete and value create / update / delete, including
their domain-error → HTTP-envelope mappings (404) and the value-out assembly
branches. The service layer is covered by the ``test_admin_catalog_service_*``
files; the attribute happy path already asserted in ``test_admin_catalog_api.py``
is not duplicated here.
"""

import pytest
from httpx import AsyncClient

from tests.admin_catalog._router_helpers import (
    ADMIN,
    HTTP_CREATED,
    HTTP_NO_CONTENT,
    HTTP_NOT_FOUND,
    HTTP_OK,
    MISSING_ID,
    make_attribute,
    make_value,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Attributes: update / delete
# --------------------------------------------------------------------------- #
class TestAttributeHandlers:
    """Attribute update/delete handlers and their error branches."""

    async def test_update_attribute_translations_success(
        self, client: AsyncClient
    ) -> None:
        # Arrange: an existing attribute.
        attribute_id = await make_attribute(client, "size")

        # Act: replace its translations.
        resp = await client.patch(
            f"{ADMIN}/attributes/{attribute_id}",
            json={"translations": [{"lang": "ru", "name": "Размер"}]},
        )

        # Assert: the rebuilt AttributeOut carries the new translation.
        assert resp.status_code == HTTP_OK, resp.text
        names = {tr["name"] for tr in resp.json()["translations"]}
        assert "Размер" in names

    async def test_update_missing_attribute_maps_to_404(
        self, client: AsyncClient
    ) -> None:
        # Act: PATCH an attribute that does not exist.
        resp = await client.patch(
            f"{ADMIN}/attributes/{MISSING_ID}", json={"code": "nope"}
        )

        # Assert: AdminNotFoundError → not_found.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_delete_attribute_returns_204(self, client: AsyncClient) -> None:
        # Arrange: a standalone attribute.
        attribute_id = await make_attribute(client, "material")

        # Act: delete it.
        resp = await client.delete(f"{ADMIN}/attributes/{attribute_id}")

        # Assert: no-content success.
        assert resp.status_code == HTTP_NO_CONTENT, resp.text

    async def test_delete_missing_attribute_maps_to_404(
        self, client: AsyncClient
    ) -> None:
        # Act: delete a non-existent attribute.
        resp = await client.delete(f"{ADMIN}/attributes/{MISSING_ID}")

        # Assert: AdminNotFoundError → not_found.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- #
# Attribute values: create / update / delete
# --------------------------------------------------------------------------- #
class TestAttributeValueHandlers:
    """Attribute-value create/update/delete handlers and error branches."""

    async def test_create_value_missing_attribute_maps_to_404(
        self, client: AsyncClient
    ) -> None:
        # Act: create a value under an attribute that does not exist.
        resp = await client.post(
            f"{ADMIN}/attributes/{MISSING_ID}/values",
            json={"translations": [{"lang": "ru", "value": "x"}]},
        )

        # Assert: AdminNotFoundError → not_found.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_create_value_success_returns_translations(
        self, client: AsyncClient
    ) -> None:
        # Arrange: an existing attribute.
        attribute_id = await make_attribute(client, "color2")

        # Act: create a value with two translations.
        resp = await client.post(
            f"{ADMIN}/attributes/{attribute_id}/values",
            json={
                "translations": [
                    {"lang": "ru", "value": "Синий"},
                    {"lang": "ro", "value": "Albastru"},
                ]
            },
        )

        # Assert: the handler assembles the value-out with both translations.
        assert resp.status_code == HTTP_CREATED, resp.text
        langs = {tr["lang"] for tr in resp.json()["translations"]}
        assert langs == {"ru", "ro"}

    async def test_update_value_success_replaces_translations(
        self, client: AsyncClient
    ) -> None:
        # Arrange: an attribute with an existing value.
        attribute_id = await make_attribute(client, "finish")
        value_id = await make_value(client, attribute_id)

        # Act: replace the value's translations.
        resp = await client.patch(
            f"{ADMIN}/attributes/values/{value_id}",
            json={"translations": [{"lang": "ru", "value": "Матовый"}]},
        )

        # Assert: the rebuilt value-out carries the replaced translation.
        assert resp.status_code == HTTP_OK, resp.text
        values = {tr["value"] for tr in resp.json()["translations"]}
        assert "Матовый" in values

    async def test_update_missing_value_maps_to_404(self, client: AsyncClient) -> None:
        # Act: update a value that does not exist.
        resp = await client.patch(
            f"{ADMIN}/attributes/values/{MISSING_ID}",
            json={"translations": [{"lang": "ru", "value": "x"}]},
        )

        # Assert: AdminNotFoundError → not_found.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_delete_value_returns_204(self, client: AsyncClient) -> None:
        # Arrange: an attribute with a value.
        attribute_id = await make_attribute(client, "weight")
        value_id = await make_value(client, attribute_id)

        # Act: delete the value.
        resp = await client.delete(f"{ADMIN}/attributes/values/{value_id}")

        # Assert: no-content success.
        assert resp.status_code == HTTP_NO_CONTENT, resp.text

    async def test_delete_missing_value_maps_to_404(self, client: AsyncClient) -> None:
        # Act: delete a value that does not exist.
        resp = await client.delete(f"{ADMIN}/attributes/values/{MISSING_ID}")

        # Assert: AdminNotFoundError → not_found.
        assert resp.status_code == HTTP_NOT_FOUND, resp.text
        assert resp.json()["error"]["code"] == "not_found"
