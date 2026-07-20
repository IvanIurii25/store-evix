"""Search-domain test fixtures (stage B3).

Provides:

* ``client`` — a local FastAPI app that mounts *only* the search router under
  ``/api/v1`` (the integrator wires it into ``main`` separately), with
  ``get_session`` overridden to the shared transactional ``db_session`` and the
  unified error handlers registered.
* ``seed`` — helpers to insert a category tree, products, translations, media and
  attributes inside the test transaction, and to rebuild ``product_card``.

Products are inserted via ``product_translation`` rows so the DB trigger fills
``search_vector`` (per-language config) within the same transaction — the FTS
tests then exercise the real ``websearch_to_tsquery`` / ``ts_rank`` path.

The ``db_session`` fixture comes from the top-level ``tests/conftest.py``.
"""

from decimal import Decimal

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routers.search import router as search_router
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.models.catalog import (
    Attribute,
    AttributeTranslation,
    AttributeValue,
    AttributeValueTranslation,
    Category,
    CategoryTranslation,
    Media,
    Product,
    ProductAttribute,
    ProductTranslation,
)
from app.services.catalog_service import CatalogService


@pytest_asyncio.fixture
async def client(db_session):
    """Local app mounting only the search router under ``/api/v1``."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(search_router, prefix="/api/v1")

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


async def _add_category(
    session,
    *,
    category_id: int,
    parent_id: int | None,
    path: list[int],
    depth: int,
    position: int,
    slugs: dict[str, str],
    names: dict[str, str],
    is_active: bool = True,
) -> None:
    """Insert one category plus its ru/ro translations."""
    session.add(
        Category(
            id=category_id,
            parent_id=parent_id,
            path=path,
            depth=depth,
            is_active=is_active,
            position=position,
        )
    )
    for lang, slug in slugs.items():
        session.add(
            CategoryTranslation(
                category_id=category_id,
                lang=lang,
                name=names[lang],
                slug=slug,
            )
        )


async def _add_product(
    session,
    *,
    product_id: int,
    category_id: int,
    code: str,
    price: Decimal,
    qty: int,
    slugs: dict[str, str],
    names: dict[str, str],
    descriptions: dict[str, str] | None = None,
    old_price: Decimal | None = None,
    is_active: bool = True,
) -> None:
    """Insert one product plus its translations (the trigger fills search_vector)."""
    session.add(
        Product(
            id=product_id,
            category_id=category_id,
            code=code,
            price=price,
            old_price=old_price,
            qty=qty,
            is_active=is_active,
        )
    )
    for lang, slug in slugs.items():
        description = None
        if descriptions is not None:
            description = descriptions.get(lang)
        session.add(
            ProductTranslation(
                product_id=product_id,
                lang=lang,
                name=names[lang],
                slug=slug,
                description=description,
            )
        )


@pytest_asyncio.fixture
async def seed(db_session):
    """Return helpers to build search fixtures inside the test transaction."""

    async def _build_tree() -> None:
        # Root (id=1, "Электроника") -> child (id=2, "Телефоны").
        await _add_category(
            db_session,
            category_id=1,
            parent_id=None,
            path=[1],
            depth=0,
            position=0,
            slugs={"ru": "electronika", "ro": "electronice"},
            names={"ru": "Электроника", "ro": "Electronice"},
        )
        await _add_category(
            db_session,
            category_id=2,
            parent_id=1,
            path=[1, 2],
            depth=1,
            position=0,
            slugs={"ru": "telefony", "ro": "telefoane"},
            names={"ru": "Телефоны", "ro": "Telefoane"},
        )
        await db_session.flush()

    return {
        "build_tree": _build_tree,
        "add_category": _add_category,
        "add_product": _add_product,
        "session": db_session,
        "service": CatalogService(db_session),
        "models": {
            "Attribute": Attribute,
            "AttributeTranslation": AttributeTranslation,
            "AttributeValue": AttributeValue,
            "AttributeValueTranslation": AttributeValueTranslation,
            "Media": Media,
            "ProductAttribute": ProductAttribute,
        },
    }
