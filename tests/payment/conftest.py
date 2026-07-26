"""Card-payment (maib) test fixtures.

Wires a local FastAPI app mounting the cart + checkout + payments routers under
``/api/v1`` (the same public contract the storefront uses), bound to the shared
commit-safe ``db_session``. The maib transport is replaced by an in-memory
``StubMaibClient`` so no network is touched: it returns a canned ``payId`` /
``payUrl``, and — crucially — computes callback signatures with the *real*
client's algorithm, so signature verification is exercised for real end-to-end.

Card payment is enabled by setting the three maib credential settings for the
test session (``card_payment_enabled`` becomes true); individual tests can flip
them off to assert the disabled path.

Run with ``EVIX_TEST_DB=evix_test_payment``.
"""

from collections.abc import AsyncGenerator, Callable
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.cart import router as cart_router
from app.api.routers.checkout import router as checkout_router
from app.api.routers.payments import router as payments_router
from app.core.config import settings
from app.core.db import get_session
from app.core.errors import register_exception_handlers
from app.core.redis import get_redis
from app.models.catalog import Category, Product, ProductTranslation
from app.services.payment import payment_service as payment_service_module
from app.services.payment.maib_client import MaibClient

_API_V1_PREFIX = "/api/v1"
_TEST_REDIS_URL = "redis://localhost:56379/13"

# maib credentials that flip ``card_payment_enabled`` on for the test session.
_SIG_KEY = "test-sig-key"


class StubMaibClient:
    """In-memory stand-in for :class:`MaibClient` used across payment tests.

    Records refund calls, returns a fixed ``payId`` / ``payUrl`` for ``/pay``,
    and delegates signature verification to a real :class:`MaibClient` bound to
    the same signing key so the crypto path is genuinely exercised.
    """

    last_pay_id = "PAY-TEST-1"

    def __init__(self, *args, **kwargs) -> None:
        self._signer = MaibClient(signature_key=_SIG_KEY)
        self.refund_calls: list[tuple[str, float | None]] = []

    async def get_token(self) -> str:
        return "stub-token"

    async def create_payment(self, **kwargs) -> dict:
        return {
            "payId": self.last_pay_id,
            "payUrl": f"https://maib.test/checkout/{self.last_pay_id}",
            "orderId": kwargs.get("order_id"),
        }

    async def refund(self, pay_id: str, amount: float | None = None) -> dict:
        self.refund_calls.append((pay_id, amount))
        return {"payId": pay_id, "status": "OK"}

    def verify_callback_signature(self, result: dict, signature: str) -> bool:
        return self._signer.verify_callback_signature(result, signature)

    def sign(self, result: dict) -> str:
        """Sign a callback ``result`` the way maib would (test helper)."""
        return self._signer._compute_signature(result)


@pytest.fixture(autouse=True)
def _enable_card(monkeypatch) -> None:
    """Enable card payment (all three maib creds) for every payment test."""
    monkeypatch.setattr(settings, "maib_project_id", "proj", raising=False)
    monkeypatch.setattr(settings, "maib_project_secret", "secret", raising=False)
    monkeypatch.setattr(settings, "maib_signature_key", _SIG_KEY, raising=False)
    monkeypatch.setattr(settings, "storefront_base_url", "https://shop.test")
    monkeypatch.setattr(settings, "maib_callback_base_url", "https://api.test")


@pytest.fixture
def stub_maib(monkeypatch) -> StubMaibClient:
    """Replace the maib client the service constructs with a shared stub."""
    stub = StubMaibClient()
    monkeypatch.setattr(
        payment_service_module,
        "MaibClient",
        lambda *a, **k: stub,
    )
    return stub


def _override_rate_limit_redis(app: FastAPI) -> None:
    async def _override_redis() -> AsyncGenerator[Redis, None]:
        client = Redis.from_url(_TEST_REDIS_URL, decode_responses=True)
        await client.flushdb()
        try:
            yield client
        finally:
            await client.aclose()

    app.dependency_overrides[get_redis] = _override_redis


def _build_app(db_session: AsyncSession) -> FastAPI:
    """Assemble an app mounting cart + checkout + payments routers."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(cart_router, prefix=_API_V1_PREFIX)
    app.include_router(checkout_router, prefix=_API_V1_PREFIX)
    app.include_router(payments_router, prefix=_API_V1_PREFIX)

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    _override_rate_limit_redis(app)
    return app


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Guest-path HTTP client bound to the commit-safe session."""
    app = _build_app(db_session)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def add_product(db_session: AsyncSession) -> Callable:
    """Return a helper inserting an active product + ``ro`` translation."""

    async def _ensure_category() -> None:
        if await db_session.get(Category, 1) is None:
            db_session.add(
                Category(id=1, parent_id=None, path=[1], depth=0, is_active=True)
            )
            await db_session.flush()

    async def _add(
        product_id: int,
        *,
        price: Decimal,
        code: str,
        name: str = "Product",
        qty: int = 100,
    ) -> Product:
        await _ensure_category()
        product = Product(
            id=product_id,
            category_id=1,
            code=code,
            price=price,
            qty=qty,
            is_active=True,
        )
        db_session.add(product)
        db_session.add(
            ProductTranslation(
                product_id=product_id, lang="ro", name=name, slug=f"s-{code}"
            )
        )
        await db_session.flush()
        return product

    return _add
