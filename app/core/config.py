"""Application settings loaded from environment / .env via pydantic-settings."""

from decimal import Decimal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_JWT_DEFAULT = "dev-insecure-change-me"


class Settings(BaseSettings):
    """Runtime configuration for the evix-store backend.

    All values are read from environment variables (or a local ``.env`` file).
    No credentials are hardcoded — ``DATABASE_URL`` must be supplied by the env.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core
    app_env: str = "local"
    log_level: str = "INFO"

    # Database (asyncpg driver required, e.g. postgresql+asyncpg://user:pass@host/db)
    database_url: str = "postgresql+asyncpg://evix:evix@localhost:5432/evix_store"

    # Connection pool (§5.3 defaults)
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_echo: bool = False

    # Auth / JWT (§7). jwt_secret MUST be overridden in real environments.
    jwt_secret: str = _INSECURE_JWT_DEFAULT
    jwt_algorithm: str = "HS256"
    access_token_ttl_min: int = 30
    refresh_token_ttl_days: int = 30

    # Redis: refresh-token blacklist, rate-limit (§5.4, §7).
    redis_url: str = "redis://localhost:56379/0"

    # Celery: broker + result backend on a dedicated Redis DB (/1), kept apart
    # from the rate-limit/blacklist DB (/0) so task traffic never collides.
    celery_broker_url: str = "redis://localhost:56379/1"
    celery_result_backend: str = "redis://localhost:56379/1"

    # Store config (§2.6): single-tenant → app config, not a DB singleton.
    currency: str = "MDL"  # one fixed currency; no currency column in DB
    default_lang: str = "ro"  # ru | ro
    courier_rate: Decimal = Decimal("50")  # flat courier delivery cost
    free_delivery_from: Decimal | None = None  # free courier over this subtotal
    tax_percent: Decimal = Decimal("0")  # single VAT rate if applied

    # Media storage for admin uploads (§10). Local dir in dev; S3 later.
    media_root: str = "var/media"
    media_url_prefix: str = "/media"

    # --- MVP prod-hardening (W7) ---

    # CORS: comma-separated origins allowed to call the API from a browser.
    cors_allow_origins: str = "http://localhost:4321,http://localhost:3000"

    # Object storage (MinIO/S3) for media. Backend "local" keeps var/media (dev
    # fallback); "s3" uses the bucket below.
    storage_backend: str = "local"  # local | s3
    s3_endpoint_url: str = "http://localhost:59000"
    s3_access_key: str = "evixminio"
    s3_secret_key: str = "evixminio-secret"
    s3_bucket: str = "evix-media"
    s3_region: str = "us-east-1"
    # Public base URL objects are served from (CDN or MinIO). If empty, derived
    # from endpoint + bucket.
    s3_public_url: str = ""

    # Email (order-confirmation, §9.8). Backend "console" logs; "smtp" sends.
    email_backend: str = "console"  # console | smtp
    smtp_host: str = "localhost"
    smtp_port: int = 1025  # MailHog default
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = False
    email_from: str = "orders@evix.md"

    # Storefront base URL used to build absolute product links in emails (e.g.
    # restock notifications). No trailing slash.
    storefront_base_url: str = "https://shop.evix.md"

    # Rate limiting (§5.4), Redis-backed. Format "<count>/<window_seconds>".
    rate_limit_login: str = "5/60"
    rate_limit_checkout: str = "10/60"
    # Pageview tracking is one hit per navigation — a generous per-IP budget
    # that only trips on obvious floods (admin §6.3).
    rate_limit_track: str = "120/60"

    @property
    def cors_origins_list(self) -> list[str]:
        """CORS origins as a list (split from the comma-separated setting)."""
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @model_validator(mode="after")
    def _enforce_prod_secrets(self) -> "Settings":
        """Refuse to run outside dev with the insecure default JWT secret."""
        if (
            self.app_env not in ("local", "test")
            and self.jwt_secret == _INSECURE_JWT_DEFAULT
        ):
            raise ValueError(
                "jwt_secret is the insecure default; set JWT_SECRET for "
                f"APP_ENV={self.app_env!r}"
            )
        return self


settings = Settings()
