"""OpenTelemetry traces for the request path (SPEC §10.5).

The picture this is for is one request as a row of bars: how much of a 900 ms completion
was retrieval, how much was the provider, and how much was everything else. Prometheus
answers that in aggregate and cannot answer it for *the* slow request somebody is asking
about, which is the one that gets escalated.

**Spans are written by hand rather than by auto-instrumentation.** The phases SPEC §10.5
names — auth, rate limit, retrieval, assembly, upstream — are not library boundaries, so
no instrumentation package produces them; what those packages produce is a span per SQL
statement and per Redis call, which is a different and much noisier picture. Five
dependencies that monkeypatch libraries at import time is also a strange thing to add to a
service that is otherwise explicit about what it does.

**Sampling is head-based here and tail-based in the collector.** A ratio sampler decides at
the first span, which is before anything has gone wrong, so "sample everything that
errored" is not a decision this process can make — the error is discovered several hundred
milliseconds after the trace id was minted. The collector shipped in
``deploy/otel/collector.yaml`` keeps every trace carrying an error and thins the rest,
which is the same policy applied at the only place that can apply it.

With no ``OTEL_EXPORTER_ENDPOINT`` set, nothing is configured and the OpenTelemetry API's
own no-op tracer is what the instrumentation calls. That is the default, and it is what
the test suite runs against.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.logging import trace_id_var
from app.core.middleware import route_template

logger = logging.getLogger(__name__)

INSTRUMENTATION_NAME = "memory-gateway"

#: The attribute that joins a trace to everything else. ``X-Gateway-Request-Id`` is on the
#: response, in every log line, and on the request-log row; without it on the span, a trace
#: found in the tracing backend cannot be taken back to the row that explains it.
REQUEST_ID_ATTRIBUTE = "gateway.request_id"


def configure_tracing(settings: Settings) -> TracerProvider | None:
    """Install the global tracer provider, or leave the no-op one in place.

    Returns the provider so the caller can flush it on shutdown. Called once per process —
    the API in ``create_app``, the worker in its ``startup`` — and idempotent in the sense
    that OpenTelemetry refuses a second provider with a warning rather than an error.
    """
    if not settings.otel_exporter_endpoint:
        return None

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.otel_service_name or settings.service_name,
                "service.version": settings.version,
                "deployment.environment": settings.environment,
            }
        ),
        # Parent-based so a trace that arrives with a sampling decision keeps it: a client
        # or an ingress that decided to record this request gets a complete trace rather
        # than one with a hole where this service should be.
        sampler=ParentBased(
            ALWAYS_ON
            if settings.otel_sample_ratio >= 1.0
            else TraceIdRatioBased(settings.otel_sample_ratio)
        ),
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_endpoint))
    )
    trace.set_tracer_provider(provider)
    logger.info(
        "tracing configured",
        extra={
            "otel_endpoint": settings.otel_exporter_endpoint,
            "otel_sample_ratio": settings.otel_sample_ratio,
        },
    )
    return provider


def shutdown_tracing(provider: TracerProvider | None) -> None:
    """Flush whatever the batch processor is still holding.

    Worth doing rather than skipping: the spans still in the batch are the ones from the
    last few seconds before a deploy, which is exactly the window somebody investigates
    when a deploy goes wrong.
    """
    if provider is None:
        return
    try:
        provider.shutdown()
    except Exception:  # pragma: no cover - an exporter failing on the way out
        logger.warning("could not flush traces on shutdown", exc_info=True)


def tracer() -> trace.Tracer:
    return trace.get_tracer(INSTRUMENTATION_NAME)


@contextmanager
def phase(name: str, **attributes: Any) -> Iterator[Span]:
    """One named step of the request path.

    Deliberately thin. With no provider configured this is the API's no-op span, and the
    cost on the request path is a context attach and detach — which is what makes it
    acceptable to leave these in the hot path unconditionally rather than behind a flag
    nobody remembers to turn on.
    """
    with tracer().start_as_current_span(name) as span:
        if span.is_recording():
            for key, value in attributes.items():
                if value is not None:
                    span.set_attribute(key, value)
        yield span


def record_error(span: Span, error: BaseException) -> None:
    if not span.is_recording():
        return
    span.set_status(Status(StatusCode.ERROR, type(error).__name__))
    span.record_exception(error)


class TracingMiddleware:
    """The server span every other span hangs off.

    Named after the *route template* rather than the path, for the reason the metrics
    middleware labels by template: ``GET /api/v1/gateways/{gateway_id}`` is one operation
    with a latency distribution, and ``GET /api/v1/gateways/<uuid>`` is a million of them.
    The template is only known after routing, so the span is renamed on the way out —
    which OpenTelemetry supports precisely because this ordering is universal.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        with tracer().start_as_current_span(method, kind=SpanKind.SERVER) as span:
            token = None
            if span.is_recording():
                context = span.get_span_context()
                # Into the log formatter's contextvar, so every line this request writes
                # carries the trace id. Set here rather than read from OpenTelemetry inside
                # the formatter, so `app.core.logging` stays free of the dependency and
                # keeps working in a process with no tracing at all.
                token = trace_id_var.set(format(context.trace_id, "032x"))
                span.set_attribute("http.request.method", method)
                span.set_attribute("url.path", scope.get("path", ""))
                request_id = (scope.get("state") or {}).get("request_id")
                if request_id:
                    span.set_attribute(REQUEST_ID_ATTRIBUTE, str(request_id))

            status = 500

            async def send_with_status(message: Message) -> None:
                nonlocal status
                if message["type"] == "http.response.start":
                    status = int(message["status"])
                await send(message)

            try:
                await self.app(scope, receive, send_with_status)
            except Exception as error:
                record_error(span, error)
                _finish(span, scope, status)
                raise
            finally:
                if token is not None:
                    trace_id_var.reset(token)
            _finish(span, scope, status)


def _finish(span: Span, scope: Scope, status: int) -> None:
    if not span.is_recording():
        return
    span.update_name(f"{scope['method']} {route_template(scope)}")
    span.set_attribute("http.response.status_code", status)
    if status >= 500:
        # Only 5xx. A 429 or a 404 is the service working, and marking those as errors
        # would make "traces with errors" — the thing the collector keeps all of — mostly
        # rate-limited clients.
        span.set_status(Status(StatusCode.ERROR, f"HTTP {status}"))


__all__ = [
    "INSTRUMENTATION_NAME",
    "REQUEST_ID_ATTRIBUTE",
    "TracingMiddleware",
    "configure_tracing",
    "phase",
    "record_error",
    "shutdown_tracing",
    "tracer",
]
