"""Pydantic v2 schemas for the admin orders API (stage B7, §10).

The admin surface reuses the storefront :class:`~app.schemas.order.OrderOut` for
order projections (single source of truth for the order shape). This module adds
only the admin-specific pieces:

* :class:`TransitionRequest` — the body of
  ``POST /admin/orders/{number}/transition`` (a fulfilment status move, a payment
  status move, or both).
* :class:`AdminOrderList` — the list envelope ``{data, total, page, page_size}``
  (§4), specialized over ``OrderOut``.

These are narrow DTOs — no god-DTOs, only the fields the back office needs.
"""

from pydantic import BaseModel, Field, model_validator
from pydantic_core import PydanticCustomError

from app.core.pagination import Page
from app.models.order import ORDER_STATUSES, PAYMENT_STATUSES
from app.schemas.order import OrderOut

# Rendered literal sets for the field descriptions.
_STATUS_CHOICES: str = " | ".join(ORDER_STATUSES)
_PAYMENT_CHOICES: str = " | ".join(PAYMENT_STATUSES)


class TransitionRequest(BaseModel):
    """Body for ``POST /admin/orders/{number}/transition`` (§8 / §10).

    At least one axis must be supplied. Each axis is validated by the shared
    :class:`~app.services.order_service.OrderService` state machine — an illegal
    move raises a domain error rather than silently mutating the order.
    """

    to_status: str | None = Field(
        default=None,
        description=f"Target fulfilment status ({_STATUS_CHOICES}).",
    )
    to_payment_status: str | None = Field(
        default=None,
        description=f"Target payment status ({_PAYMENT_CHOICES}).",
    )

    @model_validator(mode="after")
    def _require_one_axis(self) -> "TransitionRequest":
        """Ensure at least one transition axis is present.

        Returns:
            TransitionRequest: The validated request.

        Raises:
            PydanticCustomError: If neither axis is supplied (mapped to 422 by
                FastAPI; a custom error keeps the error payload JSON-serializable
                for the unified validation handler).
        """
        if self.to_status is None and self.to_payment_status is None:
            raise PydanticCustomError(
                "transition_axis_required",
                "At least one of to_status / to_payment_status is required",
            )
        return self


# List envelope for ``GET /admin/orders`` — reuses the shared ``{data, total,
# page, page_size}`` pagination envelope, specialized over ``OrderOut`` (§4).
AdminOrderList = Page[OrderOut]
