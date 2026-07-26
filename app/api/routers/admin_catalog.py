"""Thin catalog admin-API router (stage B7).

Narrow, explicit back-office endpoints (no generic CRUD engine, §10). Every
route is gated by :func:`~app.api.deps.current_staff` (``is_staff`` → 403 for
non-staff). The router stays thin: it validates input via Pydantic, delegates to
:class:`~app.services.admin_catalog_service.AdminCatalogService`, and maps the
domain exceptions to the unified error envelope via :class:`HTTPException`.

Prefix is ``/admin`` (the integrator mounts the router; no ``/api/v1`` here).
"""

import asyncio

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_staff
from app.core.db import get_session
from app.core.images import ImageValidationError, validate_and_build_variants
from app.core.storage import get_storage
from app.models.catalog import Attribute, Category, Product
from app.models.user import AppUser
from app.schemas.admin_catalog import (
    AssetOut,
    AttributeCreate,
    AttributeOut,
    AttributeTranslationOut,
    AttributeUpdate,
    AttributeValueCreate,
    AttributeValueOut,
    AttributeValueTranslationOut,
    AttributeValueUpdate,
    CategoryCreate,
    CategoryMoveRequest,
    CategoryOut,
    CategoryReorderRequest,
    CategoryTranslationIn,
    CategoryTranslationOut,
    CategoryUpdate,
    MediaAdminOut,
    MediaListOut,
    MediaReorderRequest,
    MediaVariantBindRequest,
    ProductAttributeSetRequest,
    ProductCreate,
    ProductOut,
    ProductSearchItem,
    ProductSearchResult,
    ProductTranslationIn,
    ProductTranslationOut,
    ProductUpdate,
    VariantAdminOut,
    VariantCreate,
    VariantGenerateResult,
    VariantListOut,
    VariantUpdate,
    VariationAttributesRequest,
)
from app.schemas.restock import DemandItem, RestockWaitersOut
from app.services.admin_catalog_service import (
    AdminCatalogService,
    AdminConflictError,
    AdminNotFoundError,
    AdminValidationError,
)
from app.services.catalog_service import NotFoundError as CatalogNotFoundError
from app.services.catalog_service import PublicationError
from app.services.restock_service import RestockService

router = APIRouter(prefix="/admin", tags=["admin-catalog"])


def get_admin_catalog_service(
    session: AsyncSession = Depends(get_session),
) -> AdminCatalogService:
    """Construct the admin-catalog service bound to the request session."""
    return AdminCatalogService(session=session)


def _raise_http(exc: Exception) -> None:
    """Translate a domain exception into an :class:`HTTPException`.

    Args:
        exc: The domain exception raised by the service layer.

    Raises:
        HTTPException: With a status code and ``{code, message}`` detail that the
            global handler unpacks into the unified error envelope.
    """
    if isinstance(exc, (AdminNotFoundError, CatalogNotFoundError)):
        code, http_status = "not_found", status.HTTP_404_NOT_FOUND
    elif isinstance(exc, AdminConflictError):
        code, http_status = "conflict", status.HTTP_409_CONFLICT
    elif isinstance(exc, PublicationError):
        code, http_status = "not_publishable", status.HTTP_409_CONFLICT
    elif isinstance(exc, AdminValidationError):
        code, http_status = "invalid", status.HTTP_400_BAD_REQUEST
    else:
        raise exc
    raise HTTPException(
        status_code=http_status,
        detail={"code": code, "message": str(exc)},
    ) from exc


# --------------------------------------------------------------------------- #
# Response builders
# --------------------------------------------------------------------------- #
async def _build_category_out(
    service: AdminCatalogService,
    category: Category,
) -> CategoryOut:
    """Assemble a full category admin response."""
    translations = await service.get_category_translations(category.id)
    return CategoryOut(
        id=category.id,
        parent_id=category.parent_id,
        path=list(category.path),
        depth=category.depth,
        position=category.position,
        is_active=category.is_active,
        cover_image_url=category.cover_image_url,
        translations=[CategoryTranslationOut.model_validate(tr) for tr in translations],
    )


async def _build_product_out(
    service: AdminCatalogService,
    product: Product,
) -> ProductOut:
    """Assemble a full product admin response."""
    translations = await service.get_product_translations(product.id)
    media = await service.get_product_media(product.id)
    value_ids = await service.get_product_value_ids(product.id)
    variation_attribute_ids = await service.get_product_variation_attribute_ids(
        product.id
    )
    variants = await _build_variants_out(service, product.id)
    return ProductOut(
        id=product.id,
        category_id=product.category_id,
        code=product.code,
        price=product.price,
        old_price=product.old_price,
        qty=product.qty,
        is_active=product.is_active,
        is_featured=product.is_featured,
        has_variants=product.has_variants,
        translations=[ProductTranslationOut.model_validate(tr) for tr in translations],
        media=[MediaAdminOut.model_validate(item) for item in media],
        value_ids=value_ids,
        variation_attribute_ids=variation_attribute_ids,
        variants=variants,
    )


async def _build_variants_out(
    service: AdminCatalogService, product_id: int
) -> list[VariantAdminOut]:
    """Assemble a product's variants with their value ids + bound image id."""
    variants = await service.get_product_variants(product_id)
    ids = [v.id for v in variants]
    value_map = await service.get_variant_value_ids_map(ids)
    image_map = await service.get_variant_image_ids(ids)
    return [
        VariantAdminOut(
            id=v.id,
            product_id=v.product_id,
            code=v.code,
            price=v.price,
            old_price=v.old_price,
            qty=v.qty,
            position=v.position,
            is_active=v.is_active,
            value_ids=value_map.get(v.id, []),
            image_media_id=image_map.get(v.id),
        )
        for v in variants
    ]


async def _build_attribute_out(
    service: AdminCatalogService,
    attribute: Attribute,
) -> AttributeOut:
    """Assemble a full attribute admin response (translations + values)."""
    _, translations, value_rows = await service.get_attribute_detail(attribute.id)
    return AttributeOut(
        id=attribute.id,
        code=attribute.code,
        translations=[
            AttributeTranslationOut.model_validate(tr) for tr in translations
        ],
        values=[
            AttributeValueOut(
                id=value.id,
                attribute_id=value.attribute_id,
                translations=[
                    AttributeValueTranslationOut.model_validate(vt)
                    for vt in value_translations
                ],
            )
            for value, value_translations in value_rows
        ],
    )


# --------------------------------------------------------------------------- #
# Categories
# --------------------------------------------------------------------------- #
@router.get("/categories", response_model=list[CategoryOut])
async def list_categories(
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> list[CategoryOut]:
    """Return every category (active or not) as a flat, tree-orderable list."""
    categories = await service.list_categories()
    translations = await service.get_category_out_data(categories)
    return [
        CategoryOut(
            id=category.id,
            parent_id=category.parent_id,
            path=list(category.path),
            depth=category.depth,
            position=category.position,
            is_active=category.is_active,
            cover_image_url=category.cover_image_url,
            translations=[
                CategoryTranslationOut.model_validate(tr)
                for tr in translations.get(category.id, [])
            ],
        )
        for category in categories
    ]


@router.post(
    "/categories",
    response_model=CategoryOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_category(
    payload: CategoryCreate,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> CategoryOut:
    """Create a category with its initial translations."""
    try:
        category = await service.create_category(payload)
    except (AdminNotFoundError, CatalogNotFoundError) as exc:
        _raise_http(exc)
    return await _build_category_out(service, category)


@router.patch("/categories/{category_id}", response_model=CategoryOut)
async def update_category(
    category_id: int,
    payload: CategoryUpdate,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> CategoryOut:
    """Update a category's structural fields."""
    try:
        category = await service.update_category(category_id, payload)
    except (AdminNotFoundError, AdminValidationError, CatalogNotFoundError) as exc:
        _raise_http(exc)
    return await _build_category_out(service, category)


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: int,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> None:
    """Delete an empty category (no children / products)."""
    try:
        await service.delete_category(category_id)
    except (AdminNotFoundError, AdminConflictError) as exc:
        _raise_http(exc)


@router.put(
    "/categories/{category_id}/translations",
    response_model=CategoryTranslationOut,
)
async def set_category_translation(
    category_id: int,
    payload: CategoryTranslationIn,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> CategoryTranslationOut:
    """Upsert one language translation of a category."""
    try:
        translation = await service.set_category_translation(category_id, payload)
    except AdminNotFoundError as exc:
        _raise_http(exc)
    return CategoryTranslationOut.model_validate(translation)


@router.post("/categories/reorder", status_code=status.HTTP_204_NO_CONTENT)
async def reorder_categories(
    payload: CategoryReorderRequest,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> None:
    """Assign new ``position`` values to a set of sibling categories."""
    try:
        await service.reorder_categories(payload.items)
    except AdminNotFoundError as exc:
        _raise_http(exc)


@router.post("/categories/{category_id}/move", response_model=CategoryOut)
async def move_category(
    category_id: int,
    payload: CategoryMoveRequest,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> CategoryOut:
    """Re-parent a category and recompute ``path``/``depth`` for its subtree."""
    try:
        category = await service.move_category(category_id, payload.parent_id)
    except (AdminNotFoundError, AdminValidationError) as exc:
        _raise_http(exc)
    return await _build_category_out(service, category)


# --------------------------------------------------------------------------- #
# Products
# --------------------------------------------------------------------------- #
@router.get("/products", response_model=ProductSearchResult)
async def search_products(
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
    search: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    is_active: bool | None = Query(
        default=None,
        description="Restrict to active (true) / inactive (false) products.",
    ),
    low_stock: bool = Query(
        default=False,
        description="Only products at or below the low-stock threshold.",
    ),
    on_sale: bool = Query(
        default=False,
        description="Only products on sale (old_price set and > price).",
    ),
) -> ProductSearchResult:
    """Search back-office products by ``code`` or translated name, with filters."""
    rows = await service.search_products(
        search,
        limit,
        is_active=is_active,
        low_stock=low_stock,
        on_sale=on_sale,
    )
    return ProductSearchResult(
        data=[
            ProductSearchItem(
                id=product.id,
                code=product.code,
                price=product.price,
                old_price=product.old_price,
                is_active=product.is_active,
                name=name,
            )
            for product, name in rows
        ]
    )


@router.post(
    "/products",
    response_model=ProductOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_product(
    payload: ProductCreate,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> ProductOut:
    """Create a product with its initial translations."""
    try:
        product = await service.create_product(payload)
    except (AdminNotFoundError, CatalogNotFoundError, PublicationError) as exc:
        _raise_http(exc)
    return await _build_product_out(service, product)


@router.get("/products/{product_id}", response_model=ProductOut)
async def get_product(
    product_id: int,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> ProductOut:
    """Return one product's full admin view (structure + translations + media)."""
    try:
        product = await service._get_product(product_id)  # noqa: SLF001
    except AdminNotFoundError as exc:
        _raise_http(exc)
    return await _build_product_out(service, product)


@router.get(
    "/products/{product_id}/restock-waiters",
    response_model=RestockWaitersOut,
)
async def get_restock_waiters(
    product_id: int,
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
) -> RestockWaitersOut:
    """Return the number of active restock waiters for a product (demand, §9)."""
    count = await RestockService(session).waiter_count(product_id)
    return RestockWaitersOut(count=count)


@router.get("/restock/demand", response_model=list[DemandItem])
async def get_restock_demand(
    _staff: AppUser = Depends(current_staff),
    session: AsyncSession = Depends(get_session),
    lang: str = Query(default="ro", description="Language code (ru|ro)."),
) -> list[DemandItem]:
    """Return the restock-demand overview: products people are waiting for (§9).

    One aggregate row per product with active restock subscriptions, carrying the
    waiter count and 7-day momentum plus enough catalog context (name, category,
    stock, price, thumbnail) for the operator to decide what to restock. Ordered
    by waiter count descending; the front-end re-sorts.
    """
    return await RestockService(session).demand_overview(lang)


@router.patch("/products/{product_id}", response_model=ProductOut)
async def update_product(
    product_id: int,
    payload: ProductUpdate,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> ProductOut:
    """Update a product; activation enforces the both-languages rule (§2.1.1)."""
    try:
        product = await service.update_product(product_id, payload)
    except (AdminNotFoundError, CatalogNotFoundError, PublicationError) as exc:
        _raise_http(exc)
    return await _build_product_out(service, product)


@router.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(
    product_id: int,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> None:
    """Delete a product with its translations, media, attributes and cards."""
    try:
        await service.delete_product(product_id)
    except AdminNotFoundError as exc:
        _raise_http(exc)


@router.put(
    "/products/{product_id}/translations",
    response_model=ProductTranslationOut,
)
async def set_product_translation(
    product_id: int,
    payload: ProductTranslationIn,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> ProductTranslationOut:
    """Upsert one language translation of a product and rebuild the card."""
    try:
        translation = await service.set_product_translation(product_id, payload)
    except (AdminNotFoundError, CatalogNotFoundError, PublicationError) as exc:
        _raise_http(exc)
    return ProductTranslationOut.model_validate(translation)


@router.put("/products/{product_id}/attributes", response_model=ProductOut)
async def set_product_attributes(
    product_id: int,
    payload: ProductAttributeSetRequest,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> ProductOut:
    """Replace a product's attribute-value links with the given set."""
    try:
        await service.set_product_attributes(product_id, payload.value_ids)
        product = await service._get_product(product_id)  # noqa: SLF001
    except AdminNotFoundError as exc:
        _raise_http(exc)
    return await _build_product_out(service, product)


@router.post(
    "/products/{product_id}/media",
    response_model=MediaAdminOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_product_media(
    product_id: int,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
    file: UploadFile = File(...),
) -> MediaAdminOut:
    """Store an uploaded image and record its :class:`Media` row."""
    try:
        media = await service.add_product_media(product_id, file)
    except (AdminNotFoundError, AdminValidationError) as exc:
        _raise_http(exc)
    return MediaAdminOut.model_validate(media)


@router.post("/assets", response_model=AssetOut, status_code=status.HTTP_201_CREATED)
async def upload_asset(
    _staff: AppUser = Depends(current_staff),
    file: UploadFile = File(...),
) -> AssetOut:
    """Store an uploaded image asset and return its public URL — no DB row.

    For inline content that needs a stable public URL but is not product gallery
    media (e.g. rehosted description-image banners). Uses the same storage
    backend (local dir or S3/MinIO) as product media.
    """
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only image uploads are allowed",
        )
    await file.seek(0)
    data = await file.read()
    try:
        variants = await asyncio.to_thread(validate_and_build_variants, data)
    except ImageValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Unsupported or too-large image",
        ) from exc
    storage = get_storage()
    url = await storage.save(
        data,
        filename=file.filename or "",
        content_type=file.content_type or "application/octet-stream",
    )
    await storage.save_variants(url, variants)
    return AssetOut(url=url)


@router.put(
    "/products/{product_id}/media/reorder",
    response_model=MediaListOut,
)
async def reorder_product_media(
    product_id: int,
    payload: MediaReorderRequest,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> MediaListOut:
    """Set the display order of a product's images (first id = main image)."""
    try:
        media = await service.reorder_product_media(product_id, payload.ordered_ids)
    except (AdminNotFoundError, AdminValidationError) as exc:
        _raise_http(exc)
    return MediaListOut(data=[MediaAdminOut.model_validate(row) for row in media])


@router.delete(
    "/products/{product_id}/media/{media_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_product_media(
    product_id: int,
    media_id: int,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> None:
    """Remove one image from a product (DB row + best-effort stored object)."""
    try:
        await service.delete_product_media(product_id, media_id)
    except AdminNotFoundError as exc:
        _raise_http(exc)


@router.put(
    "/products/{product_id}/media/{media_id}/variant",
    response_model=MediaAdminOut,
)
async def bind_media_to_variant(
    product_id: int,
    media_id: int,
    payload: MediaVariantBindRequest,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> MediaAdminOut:
    """Bind one image to a variant (or unbind to the shared gallery)."""
    try:
        media = await service.bind_media_to_variant(
            product_id, media_id, payload.variant_id
        )
    except (AdminNotFoundError, PublicationError) as exc:
        _raise_http(exc)
    return MediaAdminOut.model_validate(media)


# --------------------------------------------------------------------------- #
# Variants (variable products)
# --------------------------------------------------------------------------- #
async def _variant_out(
    service: AdminCatalogService, variant: object
) -> VariantAdminOut:
    """Build a single variant admin DTO (value ids + bound image)."""
    value_map = await service.get_variant_value_ids_map([variant.id])
    image_map = await service.get_variant_image_ids([variant.id])
    return VariantAdminOut(
        id=variant.id,
        product_id=variant.product_id,
        code=variant.code,
        price=variant.price,
        old_price=variant.old_price,
        qty=variant.qty,
        position=variant.position,
        is_active=variant.is_active,
        value_ids=value_map.get(variant.id, []),
        image_media_id=image_map.get(variant.id),
    )


@router.get("/products/{product_id}/variants", response_model=VariantListOut)
async def list_product_variants(
    product_id: int,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> VariantListOut:
    """List a product's variants (including inactive) for the back-office."""
    return VariantListOut(data=await _build_variants_out(service, product_id))


@router.put(
    "/products/{product_id}/variation-attributes",
    response_model=ProductOut,
)
async def set_variation_attributes(
    product_id: int,
    payload: VariationAttributesRequest,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> ProductOut:
    """Set (ordered) which attributes are the product's variation selectors."""
    try:
        await service.set_variation_attributes(product_id, payload.attribute_ids)
        product = await service._get_product(product_id)  # noqa: SLF001
    except (AdminNotFoundError, AdminValidationError, PublicationError) as exc:
        _raise_http(exc)
    return await _build_product_out(service, product)


@router.post(
    "/products/{product_id}/variants",
    response_model=VariantAdminOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_variant(
    product_id: int,
    payload: VariantCreate,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> VariantAdminOut:
    """Create one variant (a full combination of variation-attribute values)."""
    try:
        variant = await service.create_variant(product_id, payload)
    except (
        AdminNotFoundError,
        AdminValidationError,
        AdminConflictError,
        PublicationError,
    ) as exc:
        _raise_http(exc)
    return await _variant_out(service, variant)


@router.post(
    "/products/{product_id}/variants/generate",
    response_model=VariantGenerateResult,
    status_code=status.HTTP_201_CREATED,
)
async def generate_product_variants(
    product_id: int,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> VariantGenerateResult:
    """Create the missing cartesian combinations from the assigned values.

    Returns the variants that were created plus a count of the combinations that
    already existed and were skipped (the operation is idempotent).
    """
    try:
        created, skipped = await service.generate_variants(product_id)
    except (AdminNotFoundError, AdminValidationError, PublicationError) as exc:
        _raise_http(exc)
    return VariantGenerateResult(
        created=[await _variant_out(service, variant) for variant in created],
        skipped=skipped,
    )


@router.patch("/variants/{variant_id}", response_model=VariantAdminOut)
async def update_variant(
    variant_id: int,
    payload: VariantUpdate,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> VariantAdminOut:
    """Update a variant's price / stock / order / active flag."""
    try:
        variant = await service.update_variant(variant_id, payload)
    except (AdminNotFoundError, AdminConflictError, PublicationError) as exc:
        _raise_http(exc)
    return await _variant_out(service, variant)


@router.delete(
    "/variants/{variant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_variant(
    variant_id: int,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> None:
    """Delete a variant (its value links + image bindings)."""
    try:
        await service.delete_variant(variant_id)
    except (AdminNotFoundError, PublicationError) as exc:
        _raise_http(exc)


# --------------------------------------------------------------------------- #
# Attributes + values
# --------------------------------------------------------------------------- #
@router.get("/attributes", response_model=list[AttributeOut])
async def list_attributes(
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> list[AttributeOut]:
    """Return every attribute with its translations and values (picker source)."""
    attributes = await service.list_attributes()
    return [await _build_attribute_out(service, attribute) for attribute in attributes]


@router.post(
    "/attributes",
    response_model=AttributeOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_attribute(
    payload: AttributeCreate,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> AttributeOut:
    """Create an attribute dictionary entry with its translations."""
    attribute = await service.create_attribute(payload)
    return await _build_attribute_out(service, attribute)


@router.patch("/attributes/{attribute_id}", response_model=AttributeOut)
async def update_attribute(
    attribute_id: int,
    payload: AttributeUpdate,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> AttributeOut:
    """Update an attribute's ``code`` and/or its translations."""
    try:
        attribute = await service.update_attribute(attribute_id, payload)
    except AdminNotFoundError as exc:
        _raise_http(exc)
    return await _build_attribute_out(service, attribute)


@router.delete("/attributes/{attribute_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attribute(
    attribute_id: int,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> None:
    """Delete an attribute with its translations, values and links."""
    try:
        await service.delete_attribute(attribute_id)
    except AdminNotFoundError as exc:
        _raise_http(exc)


@router.post(
    "/attributes/{attribute_id}/values",
    response_model=AttributeValueOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_attribute_value(
    attribute_id: int,
    payload: AttributeValueCreate,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> AttributeValueOut:
    """Create a value under an attribute with its translations."""
    try:
        value = await service.create_attribute_value(attribute_id, payload)
    except AdminNotFoundError as exc:
        _raise_http(exc)
    _, _, value_rows = await service.get_attribute_detail(attribute_id)
    translations = next(
        (vt for v, vt in value_rows if v.id == value.id),
        [],
    )
    return AttributeValueOut(
        id=value.id,
        attribute_id=value.attribute_id,
        translations=[
            AttributeValueTranslationOut.model_validate(vt) for vt in translations
        ],
    )


@router.patch(
    "/attributes/values/{value_id}",
    response_model=AttributeValueOut,
)
async def update_attribute_value(
    value_id: int,
    payload: AttributeValueUpdate,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> AttributeValueOut:
    """Replace an attribute value's translations."""
    try:
        value = await service.update_attribute_value(value_id, payload)
    except AdminNotFoundError as exc:
        _raise_http(exc)
    _, _, value_rows = await service.get_attribute_detail(value.attribute_id)
    translations = next(
        (vt for v, vt in value_rows if v.id == value.id),
        [],
    )
    return AttributeValueOut(
        id=value.id,
        attribute_id=value.attribute_id,
        translations=[
            AttributeValueTranslationOut.model_validate(vt) for vt in translations
        ],
    )


@router.delete(
    "/attributes/values/{value_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_attribute_value(
    value_id: int,
    _staff: AppUser = Depends(current_staff),
    service: AdminCatalogService = Depends(get_admin_catalog_service),
) -> None:
    """Delete an attribute value, its translations and product links."""
    try:
        await service.delete_attribute_value(value_id)
    except AdminNotFoundError as exc:
        _raise_http(exc)
