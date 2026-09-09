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
class ProxyMetrics:
    """The data plane, as an operator sees it (task 18).

    ``overhead`` is the one that matters. SPEC §4.2 promises the gateway adds under 150 ms
    p95 over a bare upstream call, and this is that number measured on production traffic
    rather than in a load test: total request time minus the time the provider had the
    request. The load suite in ``deploy/loadtest`` measures the same quantity a second way,
    against a direct-to-provider baseline, because a number the service computes about
    itself should have an outside check.

    Labelled by gateway, which is the one place in this file that accepts per-tenant
    cardinality. It is bounded — a gateway is a configured object, not a user — and the
    alternative is a "per-tenant traffic" dashboard that cannot name a tenant. ``model`` is
    the *upstream* model that answered, so an A/B split shows as two series.
    """

    requests: Counter
    duration: Histogram
    overhead: Histogram


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
class ChunkingMetrics:
    """Cutting a document up, as two numbers (task 20).

    Both labelled by *strategy*, because that is the axis the answer lives on and because
    one of these strategies is not like the others. ``chunking`` was a pure CPU step for
    the whole of this product's life; under ``semantic`` it makes an embedding call per
    sentence, which turns the pipeline's cheapest phase into a network-bound one. An
    unlabelled duration averages a millisecond of ``recursive`` together with two seconds
    of ``semantic`` and hides exactly the thing worth seeing.

    ``sizes`` is the chunk-size distribution, and it is the signal that catches a strategy
    that is *working* and useless: ``semantic`` collapsing to one chunk per sentence
    because the floor is too low, or ``by_heading`` producing 40-token chunks on a
    document of many small sections. Both look like success from every other angle —
    documents indexed, no errors, chunks on the screen.

    The label values come from the strategy literal, which is a closed set, so the
    cardinality is the number of strategies and does not grow with the corpus.
    """

    duration: Histogram
    sizes: Histogram


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
class RateLimitMetrics:
    """Throttling, as four numbers (SPEC §11, task 14).

    ``rejections`` is labelled by which cap was hit and whose it was, because the two need
    different answers: a gateway hitting its own requests-per-minute is a customer to talk
    to about capacity, and one end user hitting theirs is usually a runaway loop in one
    integration. Four limit names by two scopes is eight series, which is a chart rather
    than a cardinality problem.

    ``near_limit`` is the same pair counted at 80% instead of at 100% — the signal that
    arrives before anybody is refused, and the one the dashboard warning card is built on.

    ``unavailable`` is labelled by *policy* rather than being a bare counter, so the graph
    says what the deployment did about it. Fail-open is the default and it means the shared
    upstream key is unprotected for as long as the line is above zero; that is a thing to
    alert on, not a debug counter.

    ``duration`` is what task 14's "< 2 ms p95" is checked against in production. It has no
    labels: the question is about the mechanism, not about which gateway asked, and one
    histogram is what makes the percentile answerable at all.

    Deliberately **not** here: per-gateway utilisation. A gauge labelled by gateway id is
    unbounded cardinality on a multi-tenant platform, and the number is needed by a screen
    rather than by an alert — the Limits section reads it live from the buckets themselves,
    which is also the only way it can be accurate to the second.
    """

    rejections: Counter
    near_limit: Counter
    unavailable: Counter
    duration: Histogram


@dataclass(frozen=True)
class AuditMetrics:
    """The audit trail, as one number (SPEC §10.4, task 15).

    One, because there is only one thing about this subsystem that an operator can act
    on. Events are written inside the transaction that makes the change, so "how many
    events were recorded" is "how many mutations happened" and the screen already shows
    that. What is *not* visible anywhere is an event that could not be built — a snapshot
    function raising on a shape nobody anticipated — because recording deliberately
    swallows that rather than failing somebody's save.

    An audit log with silent gaps is worse than none, because it is trusted. This counter
    is how the gap stops being silent: any value above zero means the log is incomplete,
    and the label says which action to go and look at.
    """

    failures: Counter


@dataclass(frozen=True)
class MaintenanceMetrics:
    """The data-lifecycle jobs, as four numbers (task 17).

    ``runway`` is the one an alert is written against, and it is a gauge per table rather
    than a single number because the two partitioned tables are managed together and
    failing separately is exactly the case worth catching. It goes red *before* anything
    breaks: an exhausted runway is not a slow query, it is an insert that fails, and the
    thing that fails is request logging — so the first symptom without this gauge is
    silence on the monitoring screen.

    The two pruning counters answer "is retention actually reclaiming anything", which is
    the question behind every "why is the database still growing" ticket. ``orphans`` is a
    gauge rather than a counter because it is a *level*: a number that stays above zero
    across sweeps means something is leaking, and a total would hide that under its own
    history.
    """

    runway: Gauge
    pruned_rows: Counter
    pruned_bytes: Counter
    orphans: Gauge


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
    chunking: ChunkingMetrics
    distillation: DistillationMetrics
    rate_limits: RateLimitMetrics
    audit: AuditMetrics
    maintenance: MaintenanceMetrics
    proxy: ProxyMetrics


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
        chunking=build_chunking_metrics(registry),
        distillation=build_distillation_metrics(registry),
        rate_limits=build_rate_limit_metrics(registry),
        audit=build_audit_metrics(registry),
        maintenance=build_maintenance_metrics(registry),
        proxy=build_proxy_metrics(registry),
    )


def build_proxy_metrics(registry: CollectorRegistry) -> ProxyMetrics:
    """Split out like the others, so the data plane's counters can be built in a test."""
    return ProxyMetrics(
        requests=Counter(
            "proxy_requests_total",
            "Data-plane requests by gateway, upstream model and status code.",
            labelnames=("gateway", "model", "status"),
            registry=registry,
        ),
        duration=Histogram(
            "proxy_request_duration_seconds",
            "End-to-end data-plane request duration, by gateway.",
            labelnames=("gateway",),
            # A completion is seconds, not milliseconds, and a long one is tens of
            # seconds; the default HTTP buckets would put most of them in the last bin.
            buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
            registry=registry,
        ),
        overhead=Histogram(
            "gateway_overhead_seconds",
            "Time added by the gateway: total request duration minus the upstream call.",
            labelnames=("gateway",),
            # Buckets chosen around the budget rather than around the observed spread:
            # 0.15 is in the list so the SLO is a single `histogram_quantile` away, and
            # so an alert can be written against a bucket boundary instead of an
            # interpolation between two of them.
            buckets=(0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 1.0, 2.5),
            registry=registry,
        ),
    )


def build_audit_metrics(registry: CollectorRegistry) -> AuditMetrics:
    return AuditMetrics(
        failures=Counter(
            "audit_event_failures_total",
            "Mutations that committed without their audit event, by action.",
            labelnames=("action",),
            registry=registry,
        ),
    )


def build_rate_limit_metrics(registry: CollectorRegistry) -> RateLimitMetrics:
    return RateLimitMetrics(
        rejections=Counter(
            "rate_limit_rejections_total",
            "Requests refused by a limit, by which limit and whose.",
            labelnames=("limit", "scope"),
            registry=registry,
        ),
        near_limit=Counter(
            "rate_limit_near_limit_total",
            "Requests served while already past 80% of a limit, by which limit and whose.",
            labelnames=("limit", "scope"),
            registry=registry,
        ),
        unavailable=Counter(
            "rate_limit_unavailable_total",
            "Checks that could not reach their counters, by what the deployment did: "
            "fail_open served the request, fail_closed refused it.",
            labelnames=("policy",),
            registry=registry,
        ),
        duration=Histogram(
            "rate_limit_check_duration_seconds",
            "How long one check-and-consume took, Redis round trip included.",
            # An order of magnitude below every other histogram here, because the budget
            # being watched is 2 ms and the default buckets would put every observation
            # in the first bin.
            buckets=(0.0001, 0.00025, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.1),
            registry=registry,
        ),
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


def build_chunking_metrics(registry: CollectorRegistry) -> ChunkingMetrics:
    """Split out like the others, so a pipeline can be built in a test on its own."""
    return ChunkingMetrics(
        duration=Histogram(
            "chunking_duration_seconds",
            "How long cutting one document up took, by strategy.",
            labelnames=("strategy",),
            # The bottom edges are where the CPU-only strategies live and the top ones are
            # where `semantic` lives once it is embedding a sentence at a time. A single
            # scale spanning both is the point: the chart has to show one turning into the
            # other after somebody changes a dropdown.
            buckets=(0.001, 0.005, 0.025, 0.1, 0.5, 1.0, 2.5, 5.0, 15.0, 30.0, 60.0),
            registry=registry,
        ),
        sizes=Histogram(
            "chunk_size_tokens",
            "Tokens per produced chunk, by strategy.",
            labelnames=("strategy",),
            # Straddles the whole legal range of `chunk_size` (50..4000) with enough edges
            # near the bottom to make "this strategy is producing one sentence per chunk"
            # visible, which is the failure worth catching.
            buckets=(16, 32, 64, 128, 256, 512, 1000, 2000, 4000),
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


def build_maintenance_metrics(registry: CollectorRegistry) -> MaintenanceMetrics:
    """Split out like the others, so a job can be built in a test without an application."""
    return MaintenanceMetrics(
        runway=Gauge(
            "partition_runway_days",
            "Consecutive days of partitions that exist ahead of today, per table.",
            labelnames=("table",),
            registry=registry,
        ),
        pruned_rows=Counter(
            "retention_pruned_rows_total",
            "Request-log and transcript rows removed by retention.",
            registry=registry,
        ),
        pruned_bytes=Counter(
            "retention_pruned_bytes_total",
            "Approximate body bytes reclaimed by retention.",
            registry=registry,
        ),
        orphans=Gauge(
            "orphaned_objects",
            "Vectors and stored objects with no row behind them, at the last sweep.",
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
