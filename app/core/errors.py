"""Application error hierarchy and the single place where errors become HTTP responses.

Control-plane routes use the envelope below. The proxy routes must instead return the
*OpenAI* error shape; task 02 registers its own handler for that subtree rather than
changing this one.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_request_id

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base class for expected failures with a known HTTP mapping."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Validation(AppError):
    status_code = 422
    code = "validation_error"


class UpstreamError(AppError):
    status_code = 502
    code = "upstream_error"


def request_id_of(request: Request | None) -> str | None:
    """The correlation id for this request.

    Read from the scope first: ``ServerErrorMiddleware`` sits *outside* the request-id
    middleware, so by the time an unhandled exception reaches its handler the contextvar
    has already been reset. The scope survives.
    """
    if request is not None:
        scope_state = request.scope.get("state") or {}
        stored = scope_state.get("request_id")
        if stored:
            return str(stored)
    return get_request_id()


def error_response(
    request: Request | None = None,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id_of(request),
        }
    }
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body)


async def handle_app_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )


async def handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    return error_response(
        request,
        status_code=exc.status_code,
        code=_http_code_name(exc.status_code),
        message=str(exc.detail),
    )


async def handle_request_validation(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return error_response(
        request,
        status_code=422,
        code="validation_error",
        message="Request validation failed",
        details={"errors": _jsonable_errors(exc)},
    )


async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    # Never leak internals to the caller; the traceback goes to the log with the request id.
    logger.exception(
        "unhandled exception",
        extra={
            "path": request.url.path,
            "method": request.method,
            "request_id": request_id_of(request),
        },
        exc_info=exc,
    )
    return error_response(
        request,
        status_code=500,
        code="internal_error",
        message="An internal error occurred.",
    )


def _http_code_name(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        413: "payload_too_large",
        422: "validation_error",
        429: "rate_limited",
        503: "service_unavailable",
    }.get(status_code, "http_error")


def _jsonable_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for error in exc.errors():
        item = {k: v for k, v in error.items() if k != "ctx"}
        item["loc"] = [str(part) for part in error.get("loc", ())]
        cleaned.append(item)
    return cleaned


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_request_validation)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unexpected)
