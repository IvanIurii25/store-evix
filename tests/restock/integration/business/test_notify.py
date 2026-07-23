"""Tests for the restock notification impl (Phase 1, §4 / §7).

Exercises :meth:`RestockService.notify_product` directly against the test session
(the async impl is awaited straight — NOT through the Celery ``.delay()`` /
``asyncio.run`` bridge, which cannot run inside pytest's already-running loop).
Asserts the console email side effect fired and the subscription flipped to
``notified``.
"""

import logging

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import email
from app.models.restock import STATUS_NOTIFIED, RestockSubscription
from app.repositories.restock_repo import RestockRepository
from app.services.restock_service import RestockService
from tests.restock.conftest import TEST_USER_ID, make_product

pytestmark = pytest.mark.asyncio


async def _seed_active_sub(
    db_session: AsyncSession,
    product_id: int,
    lang: str = "ru",
) -> RestockSubscription:
    """Persist an active subscription for the fixed test user."""
    sub = RestockSubscription(
        product_id=product_id,
        user_id=TEST_USER_ID,
        lang=lang,
    )
    db_session.add(sub)
    await db_session.flush()
    return sub


async def test_notify_product_sends_and_marks(
    db_session: AsyncSession,
    customer,  # noqa: ANN001 — fixture persists the recipient user
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """notify_product emails the subscriber (console) and marks the sub notified."""
    monkeypatch.setattr(email.settings, "email_backend", email.BACKEND_CONSOLE)
    monkeypatch.setattr(email.settings, "storefront_base_url", "https://shop.evix.md")
    product = await make_product(db_session, code="notify-1", qty=5, slug="lamp")
    sub = await _seed_active_sub(db_session, product.id, lang="ru")

    with caplog.at_level(logging.INFO, logger="app.core.email"):
        sent = await RestockService(db_session).notify_product(db_session, product.id)

    assert sent == 1
    # The console backend logged the recipient, subject and the product link.
    logged = caplog.text
    assert "email(console)" in logged
    assert "waiter@example.com" in logged
    assert "https://shop.evix.md/ru/p/lamp-ru" in logged

    await db_session.refresh(sub)
    assert sub.status == STATUS_NOTIFIED
    assert sub.notified_at is not None


async def test_notify_product_no_active_subs_noop(
    db_session: AsyncSession,
) -> None:
    """With no active subscribers notify_product is a no-op returning 0."""
    product = await make_product(db_session, code="notify-empty", qty=5)
    assert await RestockService(db_session).notify_product(db_session, product.id) == 0


async def test_notify_product_skips_missing_translation(
    db_session: AsyncSession,
    customer,  # noqa: ANN001
) -> None:
    """A subscriber whose lang has no product translation is skipped (not marked)."""
    product = await make_product(
        db_session, code="notify-no-tr", qty=5, with_translations=False
    )
    await _seed_active_sub(db_session, product.id, lang="ru")

    sent = await RestockService(db_session).notify_product(db_session, product.id)
    assert sent == 0
    # Still active — nothing was sent for it.
    row = await RestockRepository(db_session).get(product.id, TEST_USER_ID)
    assert row.status != STATUS_NOTIFIED
