"""Direct repository tests for :class:`RestockRepository` (Phase 1).

Exercises the query/write methods against the transactional ``db_session``
(no HTTP, no service layer), covering the branches the API/service tests do
not reach directly: ``upsert_active`` create vs. reactivate vs. active-noop,
``delete`` present-path, ``mark_notified`` (empty + real), the
active-subscriber and account-view joins, waiter count, and the sweep
candidate query (active + in stock).
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.restock import STATUS_ACTIVE, STATUS_NOTIFIED
from app.repositories.restock_repo import RestockRepository
from tests.factories import (
    create_product,
    create_restock_subscription,
    create_user,
)

pytestmark = pytest.mark.asyncio

# Language used for every seeded subscription unless a test overrides it.
_LANG_RU: str = "ru"
# A second language used to prove ``lang`` is refreshed on reactivation.
_LANG_RO: str = "ro"
# Product stock levels expressed as named constants (AAA readability).
_IN_STOCK_QTY: int = 5
_OUT_OF_STOCK_QTY: int = 0


class TestRestockRepositoryUpsert:
    """``upsert_active`` create / reactivate / active-noop branches (§5)."""

    async def test_upsert_creates_active_when_absent(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: a user + product with no existing subscription.
        user = await create_user(db_session)
        product = await create_product(db_session, qty=_OUT_OF_STOCK_QTY)
        repo = RestockRepository(db_session)

        # Act: first upsert creates a brand-new active row.
        subscription = await repo.upsert_active(product.id, user.id, _LANG_RU)

        # Assert: row was created, active, with the given lang and no id clash.
        assert subscription.id is not None, "new subscription must be flushed"
        assert subscription.status == STATUS_ACTIVE, "new sub must be active"
        assert subscription.lang == _LANG_RU, "lang must be stored as passed"
        assert subscription.notified_at is None, "fresh sub is not notified"

    async def test_upsert_reactivates_spent_and_refreshes_lang(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: an existing NOTIFIED (spent) subscription in another lang.
        user = await create_user(db_session)
        product = await create_product(db_session, qty=_OUT_OF_STOCK_QTY)
        spent = await create_restock_subscription(
            db_session,
            product=product,
            user=user,
            status=STATUS_NOTIFIED,
            lang=_LANG_RO,
            notified_at=None,
        )
        spent.status = STATUS_NOTIFIED
        await db_session.flush()
        repo = RestockRepository(db_session)

        # Act: re-subscribing reactivates the same row and refreshes the lang.
        reactivated = await repo.upsert_active(product.id, user.id, _LANG_RU)

        # Assert: same row id, flipped back to active, lang updated, cleared.
        assert reactivated.id == spent.id, "reactivation must reuse the row"
        assert reactivated.status == STATUS_ACTIVE, "spent row must become active"
        assert reactivated.lang == _LANG_RU, "lang must refresh to latest"
        assert reactivated.notified_at is None, "notified_at must be cleared"

    async def test_upsert_active_row_only_refreshes_lang(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: an already-active subscription in ru.
        user = await create_user(db_session)
        product = await create_product(db_session, qty=_OUT_OF_STOCK_QTY)
        existing = await create_restock_subscription(
            db_session, product=product, user=user, lang=_LANG_RU
        )
        repo = RestockRepository(db_session)

        # Act: upserting again with a new lang leaves it active, updates lang.
        result = await repo.upsert_active(product.id, user.id, _LANG_RO)

        # Assert: unchanged identity/state, lang refreshed to the latest value.
        assert result.id == existing.id, "active row must be reused unchanged"
        assert result.status == STATUS_ACTIVE, "active row stays active"
        assert result.lang == _LANG_RO, "lang refreshes to latest storefront lang"


class TestRestockRepositoryWrites:
    """``delete`` and ``mark_notified`` write paths."""

    async def test_delete_removes_existing_subscription(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: an existing subscription to delete.
        user = await create_user(db_session)
        product = await create_product(db_session, qty=_OUT_OF_STOCK_QTY)
        await create_restock_subscription(db_session, product=product, user=user)
        repo = RestockRepository(db_session)

        # Act: delete it.
        await repo.delete(product.id, user.id)

        # Assert: the row is gone.
        assert await repo.get(product.id, user.id) is None, "row must be deleted"

    async def test_delete_absent_is_noop(self, db_session: AsyncSession) -> None:
        # Arrange: a user + product with no subscription at all.
        user = await create_user(db_session)
        product = await create_product(db_session, qty=_OUT_OF_STOCK_QTY)
        repo = RestockRepository(db_session)

        # Act + Assert: deleting a missing row does not raise.
        await repo.delete(product.id, user.id)
        assert await repo.get(product.id, user.id) is None, "still absent, no error"

    async def test_mark_notified_empty_ids_is_noop(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: an active subscription that must remain untouched.
        user = await create_user(db_session)
        product = await create_product(db_session, qty=_IN_STOCK_QTY)
        sub = await create_restock_subscription(db_session, product=product, user=user)
        repo = RestockRepository(db_session)

        # Act: mark_notified with an empty list returns early (no UPDATE).
        await repo.mark_notified([])

        # Assert: the subscription is still active (nothing was flipped).
        await db_session.refresh(sub)
        assert sub.status == STATUS_ACTIVE, "empty ids must not touch any row"

    async def test_mark_notified_flips_status_and_timestamp(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: an active subscription.
        user = await create_user(db_session)
        product = await create_product(db_session, qty=_IN_STOCK_QTY)
        sub = await create_restock_subscription(db_session, product=product, user=user)
        repo = RestockRepository(db_session)

        # Act: mark it notified by id.
        await repo.mark_notified([sub.id])

        # Assert: status flipped and notified_at was stamped by func.now().
        await db_session.refresh(sub)
        assert sub.status == STATUS_NOTIFIED, "id in list must become notified"
        assert sub.notified_at is not None, "notified_at must be stamped"


class TestRestockRepositoryReads:
    """Read joins: active-for-product, account view, count, sweep candidates."""

    async def test_list_active_for_product_pairs_email(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: one active + one notified sub for the same product.
        product = await create_product(db_session, qty=_IN_STOCK_QTY)
        active_user = await create_user(db_session, email="active@example.com")
        notified_user = await create_user(db_session, email="notified@example.com")
        await create_restock_subscription(db_session, product=product, user=active_user)
        await create_restock_subscription(
            db_session,
            product=product,
            user=notified_user,
            status=STATUS_NOTIFIED,
        )
        repo = RestockRepository(db_session)

        # Act: list active subscribers joined to their email.
        rows = await repo.list_active_for_product(product.id)

        # Assert: only the active subscriber, paired with the right email.
        assert len(rows) == 1, "only active subscriptions must be returned"
        _subscription, email = rows[0]
        assert email == "active@example.com", "email must join from the user row"

    async def test_list_active_for_user_joins_translation(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: a user with an active sub to a product that has ru/ro trans.
        user = await create_user(db_session)
        product = await create_product(db_session, qty=_OUT_OF_STOCK_QTY)
        await create_restock_subscription(
            db_session, product=product, user=user, lang=_LANG_RU
        )
        repo = RestockRepository(db_session)

        # Act: list the user's active subscriptions resolved in ru.
        rows = await repo.list_active_for_user(user.id, _LANG_RU)

        # Assert: one pair, with the translation joined for the requested lang.
        assert len(rows) == 1, "the active subscription must be returned"
        subscription, translation = rows[0]
        assert subscription.product_id == product.id, "pair must reference product"
        assert translation.lang == _LANG_RU, "translation must be in requested lang"

    async def test_count_active_for_product_ignores_notified(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: two active + one notified subscription on one product.
        product = await create_product(db_session, qty=_IN_STOCK_QTY)
        for idx in range(2):
            waiter = await create_user(db_session, email=f"c{idx}@example.com")
            await create_restock_subscription(db_session, product=product, user=waiter)
        spent_user = await create_user(db_session, email="spent@example.com")
        await create_restock_subscription(
            db_session,
            product=product,
            user=spent_user,
            status=STATUS_NOTIFIED,
        )
        repo = RestockRepository(db_session)

        # Act: count active waiters.
        count = await repo.count_active_for_product(product.id)

        # Assert: only the two active subscriptions are counted.
        assert count == 2, "notified subscriptions must not be counted"

    async def test_products_with_active_and_in_stock_filters(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: three products with different active/stock combinations.
        user = await create_user(db_session)
        # Candidate: active waiter AND in stock -> must appear.
        in_stock = await create_product(db_session, qty=_IN_STOCK_QTY)
        await create_restock_subscription(db_session, product=in_stock, user=user)
        # Active waiter but OUT of stock -> excluded.
        oos = await create_product(db_session, qty=_OUT_OF_STOCK_QTY)
        user_b = await create_user(db_session, email="b@example.com")
        await create_restock_subscription(db_session, product=oos, user=user_b)
        # In stock but only a NOTIFIED (spent) waiter -> excluded.
        notified_only = await create_product(db_session, qty=_IN_STOCK_QTY)
        user_c = await create_user(db_session, email="c@example.com")
        await create_restock_subscription(
            db_session,
            product=notified_only,
            user=user_c,
            status=STATUS_NOTIFIED,
        )
        repo = RestockRepository(db_session)

        # Act: fetch sweep candidates (active + qty > 0).
        product_ids = await repo.products_with_active_and_in_stock()

        # Assert: exactly the in-stock, actively-waited product.
        assert product_ids == [in_stock.id], "only active+in-stock must qualify"

    async def test_products_with_active_and_in_stock_distinct(
        self, db_session: AsyncSession
    ) -> None:
        # Arrange: one in-stock product with TWO active waiters (dedupe check).
        product = await create_product(db_session, qty=_IN_STOCK_QTY)
        for idx in range(2):
            waiter = await create_user(db_session, email=f"d{idx}@example.com")
            await create_restock_subscription(db_session, product=product, user=waiter)
        repo = RestockRepository(db_session)

        # Act: fetch sweep candidates.
        product_ids = await repo.products_with_active_and_in_stock()

        # Assert: the product id appears exactly once (DISTINCT).
        assert product_ids == [product.id], "duplicate waiters must dedupe to one"
