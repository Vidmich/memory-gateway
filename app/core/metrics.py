"""Prometheus metrics.

A dedicated registry (rather than the global default) keeps metrics scoped to one app
instance, so tests that build several apps do not fight over duplicate collector names.
"""

from __future__ import annotations

from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

_LATENCY_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.15,  # the SPEC §4.2 gateway-overhead budget
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)


@dataclass(frozen=True)
class LogMetrics:
    """The request-log write path, as three numbers.

    Separate from :class:`Metrics` because the log service is handed exactly these and
    nothing else: it has no business touching the HTTP histograms, and a narrow struct is
    what makes that true rather than merely intended.
    """

    #: Records that never reached the database, by ``reason``: ``bodies`` (shed under
    #: queue pressure), ``record`` (queue full), ``redaction`` (the pattern budget ran
    #: out), ``write_failed`` (the insert did not land). A gap in the request log has to
    #: be a number somebody can alert on.
    dropped: Counter
    written: Counter
    queue_depth: Gauge


@dataclass(frozen=True)
class RoutingMetrics:
    """Upstream routing, as three numbers (SPEC §8.1).

    Together they answer the three questions an operator has about a multi-target
    gateway. ``attempts`` labelled ``outcome="served"`` is the per-target traffic share,
    which is what an A/B split is checked against; ``failovers`` says which upstream keeps
    forcing the chain onward, which names a model to go and fix; ``chain_length`` is
    attempts per request, and its rise is the first sign of a provider degrading before
    any request has actually failed.
    """

    attempts: Counter
    failovers: Counter
    chain_length: Histogram


@dataclass(frozen=True)
class Metrics:
    registry: CollectorRegistry
    http_requests: Counter
    http_duration: Histogram
    http_in_progress: Gauge
    build_info: Gauge
    logs: LogMetrics
    routing: RoutingMetrics


def build_metrics(*, service_name: str, version: str) -> Metrics:
    registry = CollectorRegistry()

    http_requests = Counter(
        "http_requests_total",
        "HTTP requests by route and status.",
        labelnames=("method", "route", "status"),
        registry=registry,
    )
    http_duration = Histogram(
        "http_request_duration_seconds",
        "HTTP request latency by route.",
        labelnames=("method", "route"),
        buckets=_LATENCY_BUCKETS,
        registry=registry,
    )
    http_in_progress = Gauge(
        "http_requests_in_progress",
        "In-flight HTTP requests by method (the route template is not known until "
        "routing has run).",
        labelnames=("method",),
        registry=registry,
    )
    build_info = Gauge(
        "build_info",
        "Build metadata; the value is always 1.",
        labelnames=("service", "version"),
        registry=registry,
    )
    build_info.labels(service=service_name, version=version).set(1)

    return Metrics(
        registry=registry,
        http_requests=http_requests,
        http_duration=http_duration,
        http_in_progress=http_in_progress,
        build_info=build_info,
        logs=build_log_metrics(registry),
        routing=build_routing_metrics(registry),
    )


def build_routing_metrics(registry: CollectorRegistry) -> RoutingMetrics:
    """Split out for the same reason as the log counters: a router can be built in a test
    without a whole application around it."""
    return RoutingMetrics(
        attempts=Counter(
            "routing_attempts_total",
            "Upstream attempts by routing mode, target model and outcome.",
            labelnames=("mode", "model", "outcome"),
            registry=registry,
        ),
        failovers=Counter(
            "routing_failovers_total",
            "Failovers away from a target, by the target that failed and why.",
            labelnames=("model", "error_code"),
            registry=registry,
        ),
        chain_length=Histogram(
            "routing_chain_attempts",
            "Upstream attempts made per request.",
            buckets=(1, 2, 3, 4, 5, 8),
            registry=registry,
        ),
    )


def build_log_metrics(registry: CollectorRegistry) -> LogMetrics:
    """Split out so a test can build the log counters without a whole application."""
    return LogMetrics(
        dropped=Counter(
            "logs_dropped_total",
            "Request-log records or bodies that never reached the database.",
            labelnames=("reason",),
            registry=registry,
        ),
        written=Counter(
            "logs_written_total",
            "Request-log records written.",
            registry=registry,
        ),
        queue_depth=Gauge(
            "logs_queue_depth",
            "Records waiting to be flushed.",
            registry=registry,
        ),
    )
