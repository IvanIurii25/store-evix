"""Integration tests for the admin canned-response API (support module).

Drives the staff-only ``/admin/support/canned`` router over HTTP: the list read
+ ``?lang=`` filter + auth guard, the create endpoint (201 + CannedOut +
persistence + invalid-lang 422 + write guard), the patch (200 updated + unknown
404) and the delete (204 + row gone + unknown 404). No mocks: this path uses the
real transactional ``db_session`` and never touches Redis/Telegram.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.support import SupportCannedResponse
from app.repositories.support_repo import SupportCannedRepository

pytestmark = pytest.mark.asyncio

_BASE: str = "/api/v1/admin/support/canned"


async def _seed_canned(
    db_session: AsyncSession,
    *,
    title: str = "Greeting",
    text: str = "Bună ziua!",
    lang: str = "ro",
    sort_order: int = 0,
) -> SupportCannedResponse:
    """Persist a canned response directly (arrange helper), bypassing the API."""
    canned = await SupportCannedRepository(db_session).create(
        title=title,
        text=text,
        lang=lang,
        sort_order=sort_order,
    )
    await db_session.flush()
    return canned


class AdminCannedListTest:
    """``GET /canned`` list + ``?lang=`` filter + auth guard."""

    async def test_list_returns_all_and_lang_filters(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: one ro + one ru template.
        await _seed_canned(db_session, title="ro-one", lang="ro")
        await _seed_canned(db_session, title="ru-one", lang="ru")

        # Act: list everything, then only the ru templates.
        all_resp = await client.get(_BASE)
        ru_resp = await client.get(_BASE, params={"lang": "ru"})

        # Assert: full list serializes CannedOut rows; the filter keeps only ru.
        assert all_resp.status_code == 200, all_resp.text
        all_body = all_resp.json()
        assert len(all_body) == 2, "both templates listed with no filter"
        assert "title" in all_body[0], "CannedOut is serialized"
        assert ru_resp.status_code == 200, ru_resp.text
        ru_body = ru_resp.json()
        assert [c["lang"] for c in ru_body] == ["ru"], "lang filter keeps only ru"

    async def test_list_guest_guard(self, guest_client: AsyncClient) -> None:
        # Act: an unauthenticated caller hits the staff-only list.
        resp = await guest_client.get(_BASE)

        # Assert: the current_staff guard blocks it.
        assert resp.status_code in (401, 403), resp.text


class AdminCannedCreateTest:
    """``POST /canned`` — 201 + CannedOut + persistence / 422 / guard."""

    async def test_create_valid_returns_201_and_persists(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Act: create a valid template.
        resp = await client.post(
            _BASE,
            json={
                "title": "Greeting",
                "text": "Bună ziua!",
                "lang": "ro",
                "sort_order": 3,
            },
        )

        # Assert: 201 with a CannedOut carrying id + created_at.
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["id"], "created template must carry an id"
        assert body["created_at"], "created_at must be populated (refresh gotcha)"
        assert body["lang"] == "ro", "submitted lang echoed back"

        # And it is actually persisted.
        stored = (
            await db_session.execute(
                select(SupportCannedResponse).where(
                    SupportCannedResponse.id == body["id"]
                )
            )
        ).scalar_one()
        assert stored.title == "Greeting", "template row persisted"

    async def test_create_invalid_lang_422(
        self,
        client: AsyncClient,
    ) -> None:
        # Act: submit a language outside CANNED_LANGS (the field validator).
        resp = await client.post(
            _BASE,
            json={"title": "x", "text": "y", "lang": "en"},
        )

        # Assert: the CannedIn lang validator rejects it (422).
        assert resp.status_code == 422, resp.text

    async def test_create_guest_guard(self, guest_client: AsyncClient) -> None:
        # Act: an unauthenticated caller attempts a write.
        resp = await guest_client.post(
            _BASE,
            json={"title": "x", "text": "y", "lang": "ro"},
        )

        # Assert: the current_staff guard blocks the write.
        assert resp.status_code in (401, 403), resp.text


class AdminCannedMutateTest:
    """``PATCH`` / ``DELETE /canned/{id}`` — update / remove / 404."""

    async def test_patch_updates_fields(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: a template to edit.
        canned = await _seed_canned(db_session, title="old", lang="ro")

        # Act: patch every field.
        resp = await client.patch(
            f"{_BASE}/{canned.id}",
            json={
                "title": "new",
                "text": "new body",
                "lang": "ru",
                "sort_order": 9,
            },
        )

        # Assert: 200 with the updated CannedOut.
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["title"] == "new", "title updated in the response"
        assert body["lang"] == "ru", "lang updated in the response"
        assert body["sort_order"] == 9, "sort_order updated in the response"

    async def test_patch_unknown_id_404(self, client: AsyncClient) -> None:
        # Act: patch a template that does not exist.
        resp = await client.patch(
            f"{_BASE}/999999",
            json={"title": "x", "text": "y", "lang": "ro"},
        )

        # Assert: CannedNotFoundError mapped to the domain 404.
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"

    async def test_delete_existing_returns_204_and_removes_it(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        # Arrange: a template to delete.
        canned = await _seed_canned(db_session, title="bye", lang="ro")

        # Act: delete it.
        resp = await client.delete(f"{_BASE}/{canned.id}")

        # Assert: 204 No Content, and a follow-up list no longer contains it.
        assert resp.status_code == 204, resp.text
        listed = await client.get(_BASE)
        assert listed.status_code == 200, listed.text
        assert canned.id not in [
            c["id"] for c in listed.json()
        ], "the deleted template must be gone from the list"

    async def test_delete_unknown_id_404(self, client: AsyncClient) -> None:
        # Act: delete a template that does not exist.
        resp = await client.delete(f"{_BASE}/999999")

        # Assert: CannedNotFoundError mapped to the domain 404.
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"
