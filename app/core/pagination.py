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


def paginate(
    items: list[ItemType],
    total: int,
    page: int,
    page_size: int,
) -> Page[ItemType]:
    """Wrap a list of items into a :class:`Page` envelope.

    Args:
        items: Items on the current page.
        total: Total item count across all pages.
        page: Current page number (1-based).
        page_size: Requested page size.

    Returns:
        Page[ItemType]: The pagination envelope.
    """
    return Page(data=items, total=total, page=page, page_size=page_size)
