"""Pagination envelope helper: ``{data, total, page, page_size}`` (§4)."""

from typing import Generic, TypeVar

from pydantic import BaseModel

ItemType = TypeVar("ItemType")


class Page(BaseModel, Generic[ItemType]):
    """Generic pagination envelope for list responses.

    Attributes:
        data: The list of items on the current page.
        total: Total number of items across all pages.
        page: Current page number (1-based).
        page_size: Number of items requested per page.
    """

    data: list[ItemType]
    total: int
    page: int
    page_size: int
