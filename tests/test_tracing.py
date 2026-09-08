"""OpenTelemetry spans for the request path (SPEC §10.5).

The claim being tested is that a trace answers "where did those 900 ms go" for one
specific request — which needs three things to be true, and each is easy to get subtly
wrong:

* the phases exist and are **nested** under one server span, rather than being a flat list
  that says nothing about what contained what;
* the two retrieval branches are **siblings**, because the whole claim about them is that
  they run concurrently and a single span covering both would look identical either way;
* the trace can be **taken back to the request** — the request id on the span, the trace id
  in every log line.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from starlette.responses import Response

from app.core.config import get_settings
from app.core.logging import JsonFormatter, trace_id_var
from app.core.tracing import (
    REQUEST_ID_ATTRIBUTE,
    TracingMiddleware,
    configure_tracing,
    phase,
    record_error,
)


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    """A real SDK provider writing into memory.

    Installed on the module's own tracer rather than globally: OpenTelemetry refuses a
    second global provider, and a suite that set one would leave every later test tracing
    into a dead exporter.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    original = trace.get_tracer

    def tracer_from_the_test_provider(*args: object, **kwargs: object) -> trace.Tracer:
        return provider.get_tracer("memory-gateway")

    trace.get_tracer = tracer_from_the_test_provider
    try:
        yield exporter
    finally:
        trace.get_tracer = original


def by_name(exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    return {span.name: span for span in exporter.get_finished_spans()}


# ---------------------------------------------------------------------------
# the phase helper
# ---------------------------------------------------------------------------


def test_a_phase_becomes_a_span_with_its_attributes(spans: InMemorySpanExporter) -> None:
    with phase("gateway.retrieval", **{"memory.chunks": 4}):
        pass

    span = by_name(spans)["gateway.retrieval"]
    assert span.attributes is not None
    assert span.attributes["memory.chunks"] == 4


def test_a_none_attribute_is_left_off_rather_than_recorded_as_empty(
    spans: InMemorySpanExporter,
) -> None:
    """An absent value and an empty one mean different things — "no end user" against
    "an end user whose id is blank" — and a trace backend cannot tell them apart."""
    with phase("gateway.auth", **{"gateway.slug": None, "http.route": "/g/{slug}"}):
        pass

    attributes = by_name(spans)["gateway.auth"].attributes or {}
    assert "gateway.slug" not in attributes
    assert attributes["http.route"] == "/g/{slug}"


def test_phases_nest(spans: InMemorySpanExporter) -> None:
    with phase("outer"), phase("inner"):
        pass

    finished = by_name(spans)
    assert finished["inner"].parent is not None
    assert finished["inner"].parent.span_id == finished["outer"].context.span_id


async def test_concurrent_phases_are_siblings_not_a_chain(spans: InMemorySpanExporter) -> None:
    """What makes the retrieval picture readable. `asyncio.gather` copies the context into
    each task, so two branches started together share a parent instead of one becoming the
    other's child — which is how a trace shows they overlapped."""

    async def branch(name: str) -> None:
        with phase(name):
            await asyncio.sleep(0.01)

    with phase("gateway.retrieval"):
        await asyncio.gather(branch("memory.documents"), branch("memory.facts"))

    finished = by_name(spans)
    parent = finished["gateway.retrieval"].context.span_id
    assert finished["memory.documents"].parent is not None
    assert finished["memory.facts"].parent is not None
    assert finished["memory.documents"].parent.span_id == parent
    assert finished["memory.facts"].parent.span_id == parent


def test_an_error_is_recorded_on_the_span(spans: InMemorySpanExporter) -> None:
    with phase("upstream.request") as span:
        record_error(span, TimeoutError("upstream took too long"))

    finished = by_name(spans)["upstream.request"]
    assert finished.status.status_code.name == "ERROR"
    assert finished.events, "the exception should be attached as an event"


# ---------------------------------------------------------------------------
# the server span
# ---------------------------------------------------------------------------


def traced_app() -> FastAPI:
    """FastAPI rather than bare Starlette, because only FastAPI records the matched route
    on the scope — which is what both this middleware and the metrics one read to label by
    template instead of by path."""
    app = FastAPI()

    @app.get("/g/{slug}/v1/chat")
    async def ok(slug: str) -> dict[str, bool]:
        with phase("gateway.retrieval"):
            pass
        return {"ok": True}

    @app.get("/boom")
    async def boom() -> dict[str, bool]:
        raise RuntimeError("nope")

    app.add_middleware(TracingMiddleware)
    return app


async def test_the_server_span_is_named_after_the_route_template(
    spans: InMemorySpanExporter,
) -> None:
    """By template, not by path. ``GET /g/{slug}/v1/chat`` is one operation with a latency
    distribution; ``GET /g/acme/v1/chat`` is one per tenant."""
    from httpx import ASGITransport, AsyncClient

    app = traced_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        await http.get("/g/acme/v1/chat")

    names = {span.name for span in spans.get_finished_spans()}
    assert "GET /g/{slug}/v1/chat" in names
    assert "GET /g/acme/v1/chat" not in names


async def test_the_phase_hangs_off_the_server_span(spans: InMemorySpanExporter) -> None:
    from httpx import ASGITransport, AsyncClient

    app = traced_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        await http.get("/g/acme/v1/chat")

    finished = by_name(spans)
    server = finished["GET /g/{slug}/v1/chat"]
    assert finished["gateway.retrieval"].parent is not None
    assert finished["gateway.retrieval"].parent.span_id == server.context.span_id


async def test_the_request_id_is_on_the_span(spans: InMemorySpanExporter) -> None:
    """The join between a trace and everything else: the same id is on the response, in
    every log line, and on the request-log row."""
    from httpx import ASGITransport, AsyncClient

    from app.core.middleware import RequestIdMiddleware

    app = traced_app()
    app.add_middleware(RequestIdMiddleware)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        response = await http.get("/g/acme/v1/chat", headers={"X-Gateway-Request-Id": "abc-123"})

    span = by_name(spans)["GET /g/{slug}/v1/chat"]
    assert (span.attributes or {})[REQUEST_ID_ATTRIBUTE] == "abc-123"
    assert response.headers["X-Gateway-Request-Id"] == "abc-123"


async def test_a_500_marks_the_span_as_an_error(spans: InMemorySpanExporter) -> None:
    from httpx import ASGITransport, AsyncClient

    app = traced_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as http:
        await http.get("/boom")

    span = next(s for s in spans.get_finished_spans() if s.name.endswith("/boom"))
    assert span.status.status_code.name == "ERROR"


async def test_a_429_is_not_an_error(spans: InMemorySpanExporter) -> None:
    """Marking client errors as failures would make "traces with an error" — the thing the
    collector keeps all of — mostly rate-limited clients."""
    from httpx import ASGITransport, AsyncClient

    app = FastAPI()

    @app.get("/limited")
    async def throttled() -> Response:
        return Response(status_code=429)

    app.add_middleware(TracingMiddleware)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        await http.get("/limited")

    span = spans.get_finished_spans()[0]
    assert span.status.status_code.name != "ERROR"
    assert (span.attributes or {})["http.response.status_code"] == 429


# ---------------------------------------------------------------------------
# correlation with the logs
# ---------------------------------------------------------------------------


async def test_a_log_line_written_during_a_request_carries_the_trace_id(
    spans: InMemorySpanExporter,
) -> None:
    """Which is the half of correlation that matters in practice: somebody has a log line
    and wants the trace, far more often than the other way round."""
    from httpx import ASGITransport, AsyncClient

    written: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            written.append(self.format(record))

    handler = Capture()
    handler.setFormatter(JsonFormatter(service_name="memory-gateway", version="0.1.0"))
    logger = logging.getLogger("tests.tracing")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    app = FastAPI()

    @app.get("/logs")
    async def logs() -> dict[str, bool]:
        logger.info("inside the request")
        return {"ok": True}

    app.add_middleware(TracingMiddleware)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            await http.get("/logs")
    finally:
        logger.removeHandler(handler)

    payload = json.loads(written[0])
    span = spans.get_finished_spans()[0]
    assert payload["trace_id"] == format(span.context.trace_id, "032x")


def test_a_log_line_outside_a_request_has_no_trace_id_field() -> None:
    """Omitted rather than null: a field that is always present and almost always empty is
    a column every log query has to ignore."""
    assert trace_id_var.get() is None

    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
    payload = json.loads(JsonFormatter(service_name="mg", version="0.1.0").format(record))

    assert "trace_id" not in payload


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def test_no_endpoint_means_no_provider() -> None:
    """The default, and what the whole suite runs against: the OpenTelemetry API's own
    no-op tracer, so the instrumentation costs a context attach and nothing else."""
    assert (
        configure_tracing(get_settings().model_copy(update={"otel_exporter_endpoint": ""})) is None
    )


def test_instrumentation_is_harmless_with_no_provider() -> None:
    with phase("gateway.auth", **{"gateway.slug": "demo"}) as span:
        assert not span.is_recording()
