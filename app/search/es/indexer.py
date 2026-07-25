"""Build ES documents from the ORM and (re)index / delete them.

The evix Postgres is the source of truth (unlike ecom-elastic, which pulled from
a remote OBR DB over an HTTP feed). So indexing is a direct, local denormalize:
one ES document per product carrying its ru/ro name, code, category-path names,
attribute values and description. There is no ``feed_hash`` delta machinery —
writes enqueue a targeted re-index, and a nightly full ``reindex_all`` reconciles.
"""

from collections import defaultdict

from elasticsearch import AsyncElasticsearch, NotFoundError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.catalog import (
    ALLOWED_LANGS,
    AttributeValueTranslation,
    Category,
    CategoryTranslation,
    Product,
    ProductAttribute,
    ProductTranslation,
)
from app.search.es.index import INDEX_BODY, normalize_code

_BATCH = 500
# Business-priority nudge folded into the ES score (function_score log1p). Small
# by design — a featured product edges out an equal-relevance non-featured one,
# but never outranks a materially better text match.
_FEATURED_PRIORITY = 10


async def ensure_index(client: AsyncElasticsearch) -> bool:
    """Create the index from :data:`INDEX_BODY` if it does not exist.

    Returns:
        True if the index was created, False if it already existed.
    """
    exists = await client.indices.exists(index=settings.elastic_index)
    if exists:
        return False
    await client.indices.create(index=settings.elastic_index, body=INDEX_BODY)
    return True


async def recreate_index(client: AsyncElasticsearch) -> None:
    """Drop (if present) and recreate the index with the current mapping."""
    try:
        await client.indices.delete(index=settings.elastic_index)
    except NotFoundError:
        pass
    await client.indices.create(index=settings.elastic_index, body=INDEX_BODY)


async def _fetch_docs(session: AsyncSession, product_ids: list[int]) -> list[dict]:
    """Build ES documents for the given product ids (bulk queries, no N+1)."""
    if not product_ids:
        return []

    products = (
        await session.execute(select(Product).where(Product.id.in_(product_ids)))
    ).scalars().all()
    if not products:
        return []
    pids = [p.id for p in products]

    # Names + descriptions per (product, lang).
    tr_rows = (
        await session.execute(
            select(ProductTranslation).where(ProductTranslation.product_id.in_(pids))
        )
    ).scalars().all()
    names: dict[tuple[int, str], str] = {}
    descs: dict[tuple[int, str], str] = {}
    for t in tr_rows:
        names[(t.product_id, t.lang)] = t.name or ""
        if t.description:
            descs[(t.product_id, t.lang)] = t.description

    # Category-path names per (product, lang): walk each product's category path
    # (root..self) and join the localized category names.
    cat_ids_by_product = {p.id: p.category_id for p in products}
    cat_rows = (
        await session.execute(
            select(Category).where(Category.id.in_(set(cat_ids_by_product.values())))
        )
    ).scalars().all()
    path_by_cat: dict[int, list[int]] = {c.id: (c.path or [c.id]) for c in cat_rows}
    all_path_ids = {cid for path in path_by_cat.values() for cid in path}
    cat_name: dict[tuple[int, str], str] = {}
    if all_path_ids:
        ctr = (
            await session.execute(
                select(CategoryTranslation).where(
                    CategoryTranslation.category_id.in_(all_path_ids)
                )
            )
        ).scalars().all()
        for c in ctr:
            cat_name[(c.category_id, c.lang)] = c.name

    # Attribute values per (product, lang).
    attr_rows = (
        await session.execute(
            select(
                ProductAttribute.product_id,
                AttributeValueTranslation.lang,
                AttributeValueTranslation.value,
            )
            .join(
                AttributeValueTranslation,
                AttributeValueTranslation.value_id == ProductAttribute.value_id,
            )
            .where(ProductAttribute.product_id.in_(pids))
        )
    ).all()
    attrs: dict[tuple[int, str], list[str]] = defaultdict(list)
    for product_id, lang, value in attr_rows:
        attrs[(product_id, lang)].append(value)

    docs: list[dict] = []
    for p in products:
        doc: dict = {
            "id": p.id,
            "is_active": bool(p.is_active),
            "priority": _FEATURED_PRIORITY if p.is_featured else 0,
            "code": p.code or "",
            "normalized_code": normalize_code(p.code),
        }
        cat_path = path_by_cat.get(p.category_id, [p.category_id])
        for lang in ALLOWED_LANGS:
            name = names.get((p.id, lang), "")
            doc[f"name_{lang}"] = name
            doc[f"desc_{lang}"] = descs.get((p.id, lang), "")
            doc[f"category_{lang}"] = ", ".join(
                cat_name[(cid, lang)] for cid in cat_path if (cid, lang) in cat_name
            )
            doc[f"attrs_{lang}"] = ", ".join(attrs.get((p.id, lang), []))
            doc[f"name_{lang}_completion"] = {"input": [name]} if name else None
        docs.append(doc)
    return docs


async def index_products(
    session: AsyncSession, client: AsyncElasticsearch, product_ids: list[int]
) -> int:
    """(Re)index the given products. Returns the number of documents written."""
    docs = await _fetch_docs(session, product_ids)
    if not docs:
        return 0
    operations: list[dict] = []
    for doc in docs:
        operations.append({"index": {"_id": str(doc["id"])}})
        operations.append(doc)
    await client.bulk(index=settings.elastic_index, operations=operations, refresh=False)
    return len(docs)


async def delete_products(client: AsyncElasticsearch, product_ids: list[int]) -> None:
    """Remove the given products from the index (ignore missing docs)."""
    for pid in product_ids:
        try:
            await client.delete(index=settings.elastic_index, id=str(pid))
        except NotFoundError:
            pass


async def reindex_all(session: AsyncSession, client: AsyncElasticsearch) -> int:
    """Recreate the index and index every product in batches.

    Returns:
        Total number of documents indexed.
    """
    await recreate_index(client)
    all_ids = (
        await session.execute(select(Product.id).order_by(Product.id))
    ).scalars().all()
    total = 0
    for start in range(0, len(all_ids), _BATCH):
        batch = list(all_ids[start : start + _BATCH])
        total += await index_products(session, client, batch)
    await client.indices.refresh(index=settings.elastic_index)
    return total
