"""HTTP middleware: request id, access logging, and metrics.

These are **pure ASGI** middleware rather than ``BaseHTTPMiddleware`` subclasses.
``BaseHTTPMiddleware`` runs every request inside its own task group and copies the
response body through a memory object stream on the way out; on the streaming path that
is per-frame overhead inside a budget of 150 ms total (SPEC §4.2), for no behaviour these
three need. Speaking ASGI directly also leaves the route's own ``StreamingResponse``, and
its disconnect handling, untouched.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.ids import uuid7_str
from app.core.logging import request_id_var
from app.core.metrics import Metrics

logger = logging.getLogger("app.access")

REQUEST_ID_HEADER = "X-Gateway-Request-Id"
UNMATCHED_ROUTE = "<unmatched>"


class RequestIdMiddleware:
    """Bind a correlation id for the lifetime of the request.

    An inbound id is honoured so a caller can correlate across systems; anything else
    gets a fresh UUIDv7. The id is echoed on the response, including on error responses.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        inbound = headers.get(REQUEST_ID_HEADER) or headers.get("x-request-id")
        request_id = inbound.strip()[:128] if inbound and inbound.strip() else uuid7_str()

        # Also on the scope: the exception handlers run outside this contextvar's scope
        # in some paths, and the scope survives where the contextvar does not.
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)


class AccessLogMiddleware:
    """One structured line per request, with latency and the resolved route template."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500

        async def send_with_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        except Exception:
            # The exception handler produces the response; this records that it happened,
            # with timing, then re-raises untouched.
            logger.warning("request failed", extra=_fields(scope, started))
            raise

        logger.info("request", extra={**_fields(scope, started), "status": status})


class MetricsMiddleware:
    """Record request counts and latency, labelled by route template.

    Labelling by template rather than by raw path is what keeps cardinality bounded once
    ids appear in URLs.
    """

    def __init__(self, app: ASGIApp, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        status = "500"

        async def send_with_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = str(message["status"])
            await send(message)

        # The in-flight gauge is labelled by method only: routing has not happened yet,
        # so the template is unknown here, and a gauge must be decremented under exactly
        # the label it was incremented under.
        self.metrics.http_in_progress.labels(method=method).inc()
        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_with_status)
        finally:
            elapsed = time.perf_counter() - started
            route = route_template(scope)
            self.metrics.http_in_progress.labels(method=method).dec()
            self.metrics.http_duration.labels(method=method, route=route).observe(elapsed)
            self.metrics.http_requests.labels(method=method, route=route, status=status).inc()


def route_template(scope: Scope) -> str:
    """The matched route's path template, or ``<unmatched>``.

    Starlette records the matched route on the scope during routing, so this is only
    meaningful *after* the request has been handled — which is where both callers use it.
    """
    route: Any = scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else UNMATCHED_ROUTE


def _fields(scope: Scope, started: float) -> dict[str, Any]:
    return {
        "method": scope["method"],
        "path": scope["path"],
        "route": route_template(scope),
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }
