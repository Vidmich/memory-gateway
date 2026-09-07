"""HTTP middleware: request id, access logging, and metrics."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.ids import uuid7_str
from app.core.logging import request_id_var
from app.core.metrics import Metrics

logger = logging.getLogger("app.access")

REQUEST_ID_HEADER = "X-Gateway-Request-Id"
UNMATCHED_ROUTE = "<unmatched>"

CallNext = Callable[[Request], Awaitable[Response]]


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Bind a correlation id for the lifetime of the request.

    An inbound id is honoured so a caller can correlate across systems; anything else
    gets a fresh UUIDv7. The id is echoed on the response, including on error responses.
    """

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        inbound = request.headers.get(REQUEST_ID_HEADER) or request.headers.get("X-Request-Id")
        request_id = inbound.strip()[:128] if inbound and inbound.strip() else uuid7_str()

        token = request_id_var.set(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)

        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """One structured line per request, with latency and the resolved route template."""

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The exception handler produces the response; this records that it happened,
            # with timing, then re-raises untouched.
            logger.warning(
                "request failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "route": route_template(request),
                    "duration_ms": _elapsed_ms(started),
                },
            )
            raise

        logger.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "route": route_template(request),
                "status": response.status_code,
                "duration_ms": _elapsed_ms(started),
            },
        )
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record request counts and latency, labelled by route template.

    Labelling by template rather than by raw path is what keeps cardinality bounded once
    ids appear in URLs.
    """

    def __init__(self, app: ASGIApp, metrics: Metrics) -> None:
        super().__init__(app)
        self.metrics = metrics

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        method = request.method

        # The in-flight gauge is labelled by method only: routing has not happened yet,
        # so the template is unknown here, and a gauge must be decremented under exactly
        # the label it was incremented under.
        self.metrics.http_in_progress.labels(method=method).inc()
        started = time.perf_counter()
        status = "500"
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        finally:
            elapsed = time.perf_counter() - started
            route = route_template(request)
            self.metrics.http_in_progress.labels(method=method).dec()
            self.metrics.http_duration.labels(method=method, route=route).observe(elapsed)
            self.metrics.http_requests.labels(method=method, route=route, status=status).inc()


def route_template(request: Request) -> str:
    """The matched route's path template, or ``<unmatched>``.

    Starlette records the matched route on the scope during routing, so this is only
    meaningful *after* the request has been handled — which is where both callers use it.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else UNMATCHED_ROUTE


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
