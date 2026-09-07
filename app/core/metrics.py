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
class Metrics:
    registry: CollectorRegistry
    http_requests: Counter
    http_duration: Histogram
    http_in_progress: Gauge
    build_info: Gauge


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
    )
