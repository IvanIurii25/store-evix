"""FastAPI application entrypoint: lifespan, ORJSON, routers, error handlers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routers.admin_catalog import router as admin_catalog_router
from app.api.routers.admin_orders import router as admin_orders_router
from app.api.routers.auth import router as auth_router
from app.api.routers.cart import router as cart_router
from app.api.routers.catalog import router as catalog_router
from app.api.routers.checkout import router as checkout_router
from app.api.routers.health import router as health_router
from app.api.routers.orders import router as orders_router
from app.api.routers.search import router as search_router
from app.api.routers.users import router as users_router
from app.core.config import settings
from app.core.db import dispose_engine
from app.core.errors import register_exception_handlers
from app.core.logging import setup_logging
from app.core.redis import close_redis

API_V1_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage engine-pool and Redis lifecycle.

    The async engine (and its connection pool) is created lazily at import time
    in :mod:`app.core.db`; here we dispose it and close the Redis client cleanly
    on shutdown.
    """
    yield
    await dispose_engine()
    await close_redis()


def create_app() -> FastAPI:
    """Build and configure the FastAPI application.

    Returns:
        FastAPI: The configured application instance.
    """
    logger = setup_logging()
    # FastAPI 0.139+ serializes responses to JSON bytes natively (fast path via
    # Pydantic core); no custom ORJSONResponse class needed.
    app = FastAPI(
        title="evix-store",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)
    app.include_router(health_router, prefix=API_V1_PREFIX)
    app.include_router(catalog_router, prefix=API_V1_PREFIX)
    app.include_router(search_router, prefix=API_V1_PREFIX)
    app.include_router(cart_router, prefix=API_V1_PREFIX)
    app.include_router(checkout_router, prefix=API_V1_PREFIX)
    app.include_router(orders_router, prefix=API_V1_PREFIX)
    app.include_router(auth_router, prefix=API_V1_PREFIX)
    app.include_router(users_router, prefix=API_V1_PREFIX)
    app.include_router(admin_catalog_router, prefix=API_V1_PREFIX)
    app.include_router(admin_orders_router, prefix=API_V1_PREFIX)

    # Serve admin-uploaded media locally in dev (§10; MinIO/S3 in prod — see README).
    media_dir = Path(settings.media_root)
    media_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        settings.media_url_prefix,
        StaticFiles(directory=media_dir),
        name="media",
    )
    logger.info("evix-store app configured (env=%s)", settings.app_env)
    return app


app = create_app()
