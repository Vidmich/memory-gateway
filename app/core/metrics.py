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
class JobMetrics:
    """The worker, as four numbers (task 09).

    ``completed`` is labelled by outcome rather than split into two counters, so the
    failure *rate* is one PromQL expression instead of a join. ``queue_depth`` is the one
    that pages somebody: a rising depth means ingestion is falling behind, and it says so
    before any individual document looks wrong.
    """

    started: Counter
    completed: Counter
    duration: Histogram
    dead_lettered: Counter
    queue_depth: Gauge


@dataclass(frozen=True)
class RetrievalMetrics:
    """Document retrieval, as three numbers (SPEC §6.3, task 10).

    ``attempts`` is labelled by outcome and one of those labels is the point of the whole
    struct. ``outcome="empty"`` over ``hit + empty`` is the **empty-retrieval rate**, and
    it is the only signal that distinguishes a gateway whose knowledge base answers its
    users from one that is misconfigured — a wrong connector, a score floor set too high,
    an index that never finished building. All three produce perfectly healthy-looking
    traffic: normal latency, no errors, and answers that quietly come from nowhere.

    ``skipped`` is a separate label rather than folded into ``empty`` for the same reason:
    a gateway with no connectors attached is not retrieving nothing, it is not
    retrieving, and averaging the two together would hide the misconfiguration inside the
    deliberate choice.

    ``injected_tokens`` is what memory costs, per request, in the unit providers bill in.

    ``recalls`` and ``recall_duration`` are the same two numbers for the *other* memory —
    conversation facts (task 12). Separate series rather than a ``kind`` label on the
    document ones, because the two answer different questions and share no meaningful
    aggregate: a document retrieval that finds nothing is usually a misconfiguration,
    while a fact recall that finds nothing is the ordinary state of a new end user.
    Summing them would make the empty-retrieval rate — the one signal that catches a
    broken knowledge base — climb every time a customer acquires a user.
    """

    attempts: Counter
    duration: Histogram
    injected_tokens: Histogram
    recalls: Counter
    recall_duration: Histogram


@dataclass(frozen=True)
class ExtractionMetrics:
    """Reading a file, as two numbers (task 11).

    Both are labelled by *format*, because that is the axis the answer lives on. Extraction
    is where corpus quality is decided, and it degrades one format at a time: a PDF library
    upgrade that starts returning nothing, an Office parser that chokes on files from one
    vendor's export. An unlabelled failure rate averages that into invisibility — nine
    healthy formats hide the tenth — and an unlabelled duration hides that PDFs are two
    orders of magnitude slower than Markdown, which is the fact that decides how much
    worker capacity a customer's corpus needs.

    ``completed`` is labelled by outcome rather than split in two, so the failure *rate* is
    one expression; ``skipped`` is one of those outcomes, because a corpus that is all
    scans is a support conversation rather than an incident.

    The label values come from :func:`~app.services.filetypes.format_label`, which is a
    closed map with a fallback — a sniffed media type used directly would be unbounded
    cardinality with the first exotic upload.
    """

    duration: Histogram
    completed: Counter


@dataclass(frozen=True)
class DistillationMetrics:
    """Writing memory, as three numbers (SPEC §10.1, task 13).

    ``passes`` is labelled by outcome, and the failure rate over it is the number an alert
    fires on. The other two are the ones that catch a distillation that is *working* and
    useless.

    ``dispositions`` counts what happened to each thing the extractor proposed —
    ``inserted``, ``deduped``, ``superseded``, ``rejected``, ``evicted``. Two ratios over it
    are the health signals SPEC §10.1 cannot express and this feature cannot do without.
    Dedupe near 100% means passes are succeeding and producing nothing new; supersession
    near zero on an established user means contradictions are not being caught. Both look
    exactly like success from every other angle: green jobs, no errors, facts on the screen.

    A label rather than five counters, because the useful questions are all ratios between
    them and a ratio across five series is five queries.
    """

    passes: Counter
    duration: Histogram
    dispositions: Counter


@dataclass(frozen=True)
class Metrics:
    registry: CollectorRegistry
    http_requests: Counter
    http_duration: Histogram
    http_in_progress: Gauge
    build_info: Gauge
    logs: LogMetrics
    routing: RoutingMetrics
    jobs: JobMetrics
    retrieval: RetrievalMetrics
    extraction: ExtractionMetrics
    distillation: DistillationMetrics


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
        jobs=build_job_metrics(registry),
        retrieval=build_retrieval_metrics(registry),
        extraction=build_extraction_metrics(registry),
        distillation=build_distillation_metrics(registry),
    )


def build_distillation_metrics(registry: CollectorRegistry) -> DistillationMetrics:
    return DistillationMetrics(
        passes=Counter(
            "distillation_passes_total",
            "Distillation passes by outcome: succeeded, failed, skipped.",
            labelnames=("outcome",),
            registry=registry,
        ),
        duration=Histogram(
            "distillation_duration_seconds",
            "How long one distillation pass took, model call included.",
            # Wider than retrieval's by an order of magnitude: this is a completion from a
            # cheap model plus a handful of embeddings, on a background worker where
            # seconds are ordinary and only tens of seconds are a problem.
            buckets=(0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0),
            registry=registry,
        ),
        dispositions=Counter(
            "distillation_facts_total",
            "What became of each proposed fact: inserted, deduped, superseded, rejected, evicted.",
            labelnames=("disposition",),
            registry=registry,
        ),
    )


def build_extraction_metrics(registry: CollectorRegistry) -> ExtractionMetrics:
    """Split out like the others, so a pipeline can be built in a test on its own."""
    return ExtractionMetrics(
        duration=Histogram(
            "extraction_duration_seconds",
            "How long reading one file took, by format.",
            labelnames=("format",),
            # Markdown is milliseconds and a 200-page PDF is seconds, so this spans four
            # orders of magnitude on purpose. The top edge is the default extraction
            # timeout: anything in the overflow bin did not finish.
            buckets=(0.001, 0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 15.0, 30.0, 60.0, 120.0),
            registry=registry,
        ),
        completed=Counter(
            "extractions_total",
            "Files read, by format and outcome: ok, failed, skipped.",
            labelnames=("format", "outcome"),
            registry=registry,
        ),
    )


def build_retrieval_metrics(registry: CollectorRegistry) -> RetrievalMetrics:
    """Split out like the others, so a retriever can be built in a test on its own."""
    return RetrievalMetrics(
        attempts=Counter(
            "retrieval_attempts_total",
            "Document retrievals by outcome: hit, empty, timeout, error, skipped.",
            labelnames=("outcome",),
            registry=registry,
        ),
        duration=Histogram(
            "retrieval_duration_seconds",
            "How long retrieval took, for requests where it ran.",
            # Tighter than the HTTP buckets and centred on the numbers that matter here:
            # SPEC §4.2 budgets retrieval well under 150 ms, and the default timeout is
            # 800 ms, so both need a bucket edge to sit on for a percentile to mean
            # anything near them.
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.25, 0.5, 0.8, 1.5, 3.0),
            registry=registry,
        ),
        injected_tokens=Histogram(
            "retrieval_injected_tokens",
            "Tokens of memory added to a prompt, per request.",
            buckets=(0, 100, 250, 500, 1000, 2000, 4000, 8000),
            registry=registry,
        ),
        recalls=Counter(
            "memory_recalls_total",
            "Conversation-memory recalls by outcome: hit, empty, timeout, error, skipped.",
            labelnames=("outcome",),
            registry=registry,
        ),
        recall_duration=Histogram(
            "memory_recall_duration_seconds",
            "How long conversation-memory recall took, for requests where it ran.",
            # The same edges as document retrieval, so the two can be read on one chart
            # and the slower half of `asyncio.gather` is obvious at a glance.
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.25, 0.5, 0.8, 1.5, 3.0),
            registry=registry,
        ),
    )


def build_job_metrics(registry: CollectorRegistry) -> JobMetrics:
    """Split out for the same reason as the others: a worker can be built in a test
    without a whole application around it."""
    return JobMetrics(
        started=Counter(
            "jobs_started_total",
            "Background jobs started, by job name.",
            labelnames=("job",),
            registry=registry,
        ),
        completed=Counter(
            "jobs_completed_total",
            "Background jobs that finished, by name and outcome.",
            labelnames=("job", "outcome"),
            registry=registry,
        ),
        duration=Histogram(
            "job_duration_seconds",
            "How long a job took, by name.",
            labelnames=("job",),
            # Ingestion is seconds to minutes, not milliseconds; the HTTP buckets would
            # put every job in the overflow bin.
            buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
            registry=registry,
        ),
        dead_lettered=Counter(
            "jobs_dead_lettered_total",
            "Jobs that exhausted their retries, by name.",
            labelnames=("job",),
            registry=registry,
        ),
        queue_depth=Gauge(
            "jobs_queue_depth",
            "Jobs waiting to be picked up.",
            registry=registry,
        ),
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
