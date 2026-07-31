"""Application settings loaded from environment / .env via pydantic-settings."""

from decimal import Decimal

from pydantic import field_validator, model_validator
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

    @property
    def cookie_secure(self) -> bool:
        """Mark client cookies ``Secure`` everywhere except local/test.

        Production is served over HTTPS (Cloudflare), so session/consent cookies
        must carry ``Secure``; a plain-HTTP local dev server would otherwise drop
        them. Mirrors the JWT-cookie posture (§7).
        """
        return self.app_env not in ("local", "test")

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

    # Search backend (ported ecom-elastic V3). ``elastic`` routes queries to
    # Elasticsearch; ``postgres`` uses the native FTS (``search_repo``). ES down
    # at runtime → the service falls back to Postgres regardless of this flag, so
    # a broken/empty index never takes search down. Flip to ``elastic`` only
    # after the first ``es_reindex_all`` has populated the index.
    search_backend: str = "postgres"  # elastic | postgres
    elastic_url: str = "http://localhost:9200"
    elastic_index: str = "evix_products"
    # Relevance floor (ES ``min_score``). 0 = no floor. Tune per catalog; the V3
    # service used 50, but that assumed a priority/keywords field mix we don't
    # index, so we start at 0 and raise it once the score distribution is known.
    elastic_min_score: float = 0.0
    # Hard cap on one ES round-trip (seconds) — a stalled ES must not hang the
    # request; on timeout the service falls back to Postgres FTS.
    elastic_request_timeout: float = 5.0

    @property
    def search_uses_elastic(self) -> bool:
        """True when the ES backend is selected (runtime health checked separately)."""
        return self.search_backend.strip().lower() == "elastic"

    # --- Card payment via maib e-Commerce API (env-gated) ---
    # Card is a second option next to COD. It is available ONLY when all three
    # maib credentials below are set; otherwise the option stays hidden and the
    # COD money-path is byte-for-byte unchanged (see ``card_payment_enabled``).
    # Base URL of the maibmerchants v1 REST API (no trailing slash).
    maib_base_url: str = "https://api.maibmerchants.md/v1"
    # Credentials issued by maib; empty in dev/prod-inert until provisioned.
    maib_project_id: str = ""
    maib_project_secret: str = ""
    # Callback-signature secret; used to verify the webhook payload signature.
    maib_signature_key: str = ""
    # Public base URL of THIS API, as reachable by maib's servers, used to build
    # the callback URL maib POSTs the result to (e.g. https://api.evix.md). No
    # trailing slash. Distinct from ``storefront_base_url`` (the ok/fail returns
    # go to the storefront). Empty in dev.
    maib_callback_base_url: str = ""

    @property
    def card_payment_enabled(self) -> bool:
        """True only when all three maib credentials are configured.

        When any credential is missing the card option is unavailable and the
        checkout / COD flow behaves exactly as before (no maib call is made).
        """
        return bool(
            self.maib_project_id
            and self.maib_project_secret
            and self.maib_signature_key
        )

    # Store config (§2.6): single-tenant → app config, not a DB singleton.
    currency: str = "MDL"  # one fixed currency; no currency column in DB
    default_lang: str = "ro"  # ru | ro
    courier_rate: Decimal = Decimal("50")  # flat courier delivery cost
    free_delivery_from: Decimal | None = None  # free courier over this subtotal
    tax_percent: Decimal = Decimal("0")  # single VAT rate if applied

    # --- Parcel metrics (carrier integrations, Nova Post phase P0) ---
    # Fallback weight per cart item when neither the variant nor the product has
    # a ``weight_g``. Deliberately generous: under-declaring costs us the
    # difference at the counter, over-declaring only costs the customer cents.
    parcel_default_item_weight_g: int = 500
    # Outer box used until per-product dimensions exist (millimetres).
    parcel_width_mm: int = 250
    parcel_length_mm: int = 350
    parcel_height_mm: int = 200
    # Volumetric divisor in cm³ per kg (carriers bill max(actual, volumetric)).
    # 5000 is the common courier default; the exact value comes from the tariff.
    parcel_volumetric_divisor: int = 5000

    # --- Nova Post delivery (env-gated, like maib) ---
    # Empty (the default) means the carrier is off: the storefront shows the
    # existing pickup/own-courier options and the Nova Post endpoints stay shut.
    # ``stub`` serves a deterministic fake carrier so the feature can be built
    # and tested before the merchant contract exists — it is refused outside dev
    # by the validator below. ``sandbox``/``live`` pick the real API host.
    novapost_mode: str = ""  # "" (off) | stub | sandbox | live
    novapost_api_token: str = ""  # apiKey issued by Nova Post
    novapost_contract_number: str = ""  # payerContractNumber
    # Sender: the branch parcels are handed over at, plus contact details that
    # travel on the waybill.
    novapost_sender_division_id: str = ""
    novapost_sender_name: str = ""
    novapost_sender_phone: str = ""
    novapost_sender_email: str = ""
    novapost_sender_company: str = ""
    # Which pickup categories the storefront may offer, comma-separated:
    # branch | postomat | courier.
    novapost_division_categories: str = "branch,postomat,courier"
    # Free-delivery threshold for Nova Post; falls back to the store-wide
    # ``free_delivery_from`` when unset (a carrier costs real money, so the two
    # must be separable).
    novapost_free_delivery_from: Decimal | None = None
    # Reference-data cache TTLs (seconds): settlements barely change, branch
    # lists move a little more often.
    novapost_settlements_ttl: int = 86_400
    novapost_divisions_ttl: int = 21_600

    @property
    def novapost_stub(self) -> bool:
        """True when the deterministic stub carrier is in use (no network)."""
        return self.novapost_mode.strip().lower() == "stub"

    @property
    def novapost_enabled(self) -> bool:
        """True when Nova Post may be offered at checkout.

        The stub counts as enabled — that is its whole point (build and test the
        flow before credentials exist). A real mode additionally needs the API
        token and a sender branch; a half-filled config stays disabled rather
        than failing at the first customer's checkout.
        """
        if self.novapost_stub:
            return True
        return bool(self.novapost_api_token and self.novapost_sender_division_id)

    @property
    def novapost_base_url(self) -> str:
        """Return the API host for the configured mode (empty for the stub)."""
        return {
            "sandbox": "https://api-stage.novapost.pl/v.1.0",
            "live": "https://api.novapost.com/v.1.0",
        }.get(self.novapost_mode.strip().lower(), "")

    @property
    def novapost_categories(self) -> list[str]:
        """Return the enabled pickup categories, normalized and de-duplicated."""
        seen: list[str] = []
        for raw in self.novapost_division_categories.split(","):
            item = raw.strip().lower()
            if item and item not in seen:
                seen.append(item)
        return seen

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
    # Registration is rare per human but a spam/enumeration target — a tight
    # per-IP budget blocks account-flooding and throttles 409-based email probes.
    rate_limit_register: str = "10/600"
    # Refresh is legitimately frequent (access-token rotation); a generous per-IP
    # budget only trips on obvious floods, not a NAT'd family behind one IP.
    rate_limit_refresh: str = "60/60"
    # Pageview tracking is one hit per navigation — a generous per-IP budget
    # that only trips on obvious floods (admin §6.3).
    rate_limit_track: str = "120/60"
    # Consent decisions are rare per visitor; a small per-IP budget is plenty.
    rate_limit_consent: str = "20/60"
    # Delivery reference lookups: typed into a picker, so the budget is generous
    # per IP, but present — the endpoint proxies a third-party API.
    rate_limit_delivery: str = "60/60"
    # The public Telegram webhook: per-IP budget (only Telegram's IPs should hit it).
    rate_limit_telegram: str = "60/60"

    # Telegram helpdesk (support module). telegram_bot_token is from BotFather;
    # empty in dev. telegram_webhook_secret is echoed by Telegram in the
    # X-Telegram-Bot-Api-Secret-Token header — the webhook rejects requests whose
    # header value does not match this setting.
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    # Telegram chat id of the internal staff/operators group; when set,
    # new-support-message notifications are posted there. Empty → feature off
    # (no-op).
    telegram_staff_chat_id: str = ""
    # Support conversations inactive for longer than this are purged (storage
    # limitation, LP195/2024 Art.5). ~12 months, consistent with the
    # customer-inquiry retention default.
    support_retention_days: int = 365

    @property
    def cors_origins_list(self) -> list[str]:
        """CORS origins as a list (split from the comma-separated setting)."""
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @field_validator(
        "free_delivery_from",
        "novapost_free_delivery_from",
        mode="before",
    )
    @classmethod
    def _empty_string_means_unset(cls, value: object) -> object:
        """Treat an empty env value as "not configured" rather than a parse error.

        Docker Compose expands an unset variable to an empty string, so
        ``FREE_DELIVERY_FROM: ${FREE_DELIVERY_FROM:-}`` reaches pydantic as
        ``''`` — which is not a decimal, and the container dies on import.
        Optional money settings must degrade to ``None`` instead.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

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
        if self.app_env not in ("local", "test") and self.novapost_stub:
            raise ValueError(
                "novapost_mode='stub' serves a fake carrier and must never run "
                f"outside dev; set NOVAPOST_MODE for APP_ENV={self.app_env!r}"
            )
        return self


settings = Settings()
