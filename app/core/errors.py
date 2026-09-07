"""Application error hierarchy and the single place where errors become HTTP responses.

Two envelopes, chosen by path. Control-plane routes get the gateway's own shape, which
carries the request id. Data-plane routes under ``/g/`` get the *OpenAI* error shape,
because client SDKs parse it: returning anything else there turns a useful
``AuthenticationError`` into an opaque ``APIStatusError`` at the caller.

The choice is made here, once, rather than by a second set of handlers — an unhandled
exception or a 405 from Starlette has to come out in the right shape too, and those never
reach proxy code.
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

# Every data-plane route lives under this prefix; see app/api/proxy.
DATA_PLANE_PREFIX = "/g/"


class AppError(Exception):
    """Base class for expected failures with a known HTTP mapping."""

    status_code: int = 500
    code: str = "internal_error"
    #: OpenAI ``error.type``, used when the failure surfaces on a data-plane route.
    openai_type: str | None = None

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        #: OpenAI ``error.param`` — which request field is at fault, when one is.
        self.param = param
        #: Response headers the status is meaningless without: ``Retry-After`` on a 429,
        #: ``WWW-Authenticate`` on a 401.
        self.headers = headers or {}


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


class RateLimited(AppError):
    """Too many of the same action, too quickly.

    ``Retry-After`` is not optional here: a 429 without it leaves a client guessing, and
    the guess is usually "immediately". Task 14 raises the same class from the real
    request-rate limiter.
    """

    status_code = 429
    code = "rate_limited"
    openai_type = "rate_limit_error"

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message, headers={"retry-after": str(max(1, retry_after_seconds))})
        self.retry_after_seconds = max(1, retry_after_seconds)


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


def is_data_plane(request: Request | None) -> bool:
    return request is not None and request.scope.get("path", "").startswith(DATA_PLANE_PREFIX)


def openai_error_type(status_code: int) -> str:
    if status_code == 429:
        return "rate_limit_error"
    if status_code >= 500:
        return "server_error"
    return "invalid_request_error"


def error_response(
    request: Request | None = None,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    openai_type: str | None = None,
    param: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    if is_data_plane(request):
        # The four keys are all present, `null` included: the OpenAI SDKs read them
        # positionally-by-name and some clients assume they exist.
        openai_body: dict[str, Any] = {
            "error": {
                "message": message,
                "type": openai_type or openai_error_type(status_code),
                "param": param,
                "code": code,
            }
        }
        return JSONResponse(status_code=status_code, content=openai_body, headers=headers)

    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id_of(request),
        }
    }
    if param:
        # Which field the message is about, so a form can show it next to that input
        # rather than in a banner the user then has to match up by reading. Dotted for a
        # nested one (`default_params.temperature`); the first segment is the form field.
        body["error"]["param"] = param
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body, headers=headers)


async def handle_app_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        openai_type=exc.openai_type,
        param=exc.param,
        headers=exc.headers,
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
