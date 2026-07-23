"""Unit test for the admin-catalog router's error mapper fall-through (B7, §10).

A pure synchronous test (no DB, no client) covering the ``else`` branch of
:func:`~app.api.routers.admin_catalog._raise_http`: an exception outside the
recognised domain-error set is re-raised unchanged rather than wrapped in an
``HTTPException``. Kept in its own module so it is not swept up by the
async-marked router API test files.
"""

import pytest

from app.api.routers.admin_catalog import _raise_http


class TestRaiseHttpFallThrough:
    """The error mapper re-raises anything it does not recognise (else branch)."""

    def test_unmapped_exception_is_reraised_unchanged(self) -> None:
        # Arrange: an exception type outside the domain-error set.
        sentinel = RuntimeError("boom")

        # Act + Assert: _raise_http re-raises the original, not an HTTPException.
        with pytest.raises(RuntimeError, match="boom"):
            _raise_http(sentinel)
