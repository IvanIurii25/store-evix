"""Unified error envelope and exception handlers.

Every error response follows the shape::

    {"error": {"code": "...", "message": "...", "details": <optional>}}

No stack traces are leaked to clients. Handlers are registered in ``main.py``.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class DomainError(Exception):
    """Base class for domain/service errors mapped to the unified envelope.

    Subclasses declare ``status_code`` and ``code`` as class attributes; the
    registered handler renders any instance into the
    ``{error:{code,message,details?}}`` shape. Pass structured context via
    ``details`` (e.g. ``{"product_id": 42}``).
    """

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "error"

    def __init__(self, message: str = "", *, details: Any = None) -> None:
        """Store the human message and optional structured details."""
        super().__init__(message)
        self.details = details


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
) -> JSONResponse:
    """Build an JSONResponse in the unified error envelope.

    Args:
        status_code: HTTP status code for the response.
        code: Machine-readable error code (e.g. ``"http_error"``).
        message: Human-readable error message.
        details: Optional structured details (validation errors, context).

    Returns:
        JSONResponse: Response body ``{"error": {...}}``.
    """
    payload: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        payload["details"] = details
    return JSONResponse(status_code=status_code, content={"error": payload})


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    """Wrap HTTPException into the unified envelope.

    If ``detail`` is a dict carrying a ``code`` (e.g.
    ``raise HTTPException(409, detail={"code": "out_of_stock", "message": ...})``),
    that domain code/message/details is used; otherwise a plain string detail
    maps to the generic ``http_error`` code.
    """
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        return error_response(
            status_code=exc.status_code,
            code=str(detail.get("code", "http_error")),
            message=str(detail.get("message", "")),
            details=detail.get("details"),
        )
    return error_response(
        status_code=exc.status_code,
        code="http_error",
        message=str(detail),
    )


def _jsonable_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make Pydantic v2 validation errors JSON-serializable.

    Validators that raise ``ValueError`` (slug/lang checks) put the raw
    exception object into ``ctx``; that object is not JSON serializable and
    would turn the 422 into a 500. Stringify any such context value so the
    envelope always serializes while keeping the human-readable message.

    Args:
        errors: The list returned by :meth:`RequestValidationError.errors`.

    Returns:
        list[dict[str, Any]]: The same errors with a JSON-safe ``ctx``.
    """
    safe: list[dict[str, Any]] = []
    for err in errors:
        item = dict(err)
        ctx = item.get("ctx")
        if isinstance(ctx, dict):
            item["ctx"] = {key: str(value) for key, value in ctx.items()}
        safe.append(item)
    return safe


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Wrap Pydantic v2 request-validation errors into a 422 envelope."""
    return error_response(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        code="validation_error",
        message="Request validation failed",
        details=_jsonable_errors(exc.errors()),
    )


async def domain_exception_handler(
    request: Request,
    exc: DomainError,
) -> JSONResponse:
    """Render a :class:`DomainError` into the unified envelope.

    The status code and machine-readable code come from the exception class;
    the message is ``str(exc)`` and any structured context from ``details``.
    """
    return error_response(
        status_code=exc.status_code,
        code=exc.code,
        message=str(exc),
        details=exc.details,
    )


async def unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Convert any uncaught exception into a generic 500 (no stack trace leaked)."""
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return error_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="internal_error",
        message="Internal server error",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register all unified-format exception handlers on the app."""
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(DomainError, domain_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
