"""The worker process: ``arq app.workers.main.WorkerSettings``.

Same image as the API, different command — SPEC §15 makes the application stateless and
one artefact, and a second image would be a second thing to keep at the same version.

Everything arq is told to do is here, and it is deliberately little. One function,
:func:`run_gateway_job`; ``max_tries=1``, because retries belong to
:class:`~app.services.jobs.RetryPolicy`; and a JSON serializer instead of arq's default
pickle, because a payload read out of Redis and unpickled is arbitrary code execution if
Redis is ever reachable by anyone else. The payloads are three strings; JSON costs
nothing and removes the class of problem entirely.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, cast

from arq import cron
from arq.connections import RedisSettings

from app.core.clients import Clients
from app.core.config import Settings, get_settings
from app.core.logging import bind_request_id, configure_logging
from app.core.metrics import build_metrics
from app.core.tracing import configure_tracing, shutdown_tracing
from app.services.job_queue import ARQ_FUNCTION, ARQ_HEAVY_QUEUE_KEY, ArqJobQueue, from_payload
from app.services.jobs import JobRunner
from app.services.vector_backends import VectorBackends
from app.workers.runtime import (
    Ingestion,
    Platform,
    build_dead_letters,
    build_distillation,
    build_ingestion,
    build_platform,
    build_platform_settings,
    build_runner,
    build_vector_backends,
    embedding_tokenizer,
)

logger = logging.getLogger(__name__)


async def run_gateway_job(context: dict[str, Any], payload: dict[str, Any]) -> None:
    """The single arq function. Everything else is dispatch inside the runner."""
    runner: JobRunner = context["runner"]
    request = from_payload(payload)
    # The control-plane request that caused this, bound for the duration so every log
    # line the job emits — minutes later, on another process — joins back to it.
    with bind_request_id(request.request_id):
        await runner.run(request)


async def startup(context: dict[str, Any]) -> None:
    settings: Settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service_name=f"{settings.service_name}-worker",
        version=settings.version,
    )
    # A job's spans hang off nothing — there is no inbound request — so a worker's
    # traces are their own roots, joined to the control-plane request that enqueued them
    # by `request_id` rather than by a parent span. Carrying the trace context through
    # Redis would join them properly and is deliberately not done yet: it makes a job
    # payload carry a header format, and the request id already answers the question
    # anybody actually asks.
    context["tracing"] = configure_tracing(settings)
    clients = Clients.create(settings)
    metrics = build_metrics(
        service_name=f"{settings.service_name}-worker", version=settings.version
    )
    # arq hands the worker its own pool; reusing it means the queue the runner re-enqueues
    # retries onto is the same queue this worker is reading from.
    queue = ArqJobQueue(context["redis"])
    # Read before the pipeline is built, for the same reason the API does it: the embedder
    # has to be the one the platform is configured for, not the one the environment
    # happened to name.
    platform_settings = build_platform_settings(clients, settings)
    await platform_settings.warm()
    platform_settings.start()
    backends = await build_vector_backends(clients, settings)
    context["vector_backends"] = backends
    ingestion = build_ingestion(
        clients,
        settings,
        queue=queue,
        backends=backends,
        metrics=metrics.extraction,
        chunking_metrics=metrics.chunking,
        summarization_metrics=metrics.summarization,
        embedding=platform_settings.snapshot.embedding,
        tokenizer=lambda: embedding_tokenizer(platform_settings.snapshot.embedding),
    )
    # Conversation memory's write half. Built here as well as in the API, from the same
    # function, so the pass a worker runs and the pass "Distil now" runs are the same pass.
    distillation = build_distillation(
        clients, settings, ingestion=ingestion, backends=backends, metrics=metrics.distillation
    )

    platform = build_platform(
        clients,
        settings,
        ingestion=ingestion,
        distillation=distillation,
        platform_settings=platform_settings,
        backends=backends,
        queue=queue,
        metrics=metrics.maintenance,
    )

    context["clients"] = clients
    context["ingestion"] = ingestion
    context["distillation"] = distillation
    context["platform"] = platform
    context["platform_settings"] = platform_settings
    context["runner"] = build_runner(
        ingestion,
        settings,
        dead_letters=build_dead_letters(clients),
        metrics=metrics.jobs,
        distillation=distillation,
        platform=platform,
    )
    logger.info("worker started", extra={"environment": settings.environment})


async def nightly_maintenance(context: dict[str, Any]) -> None:
    """The scheduled half of task 17, as one function so it runs in one order.

    Partitions before retention, because retention drops days and the runway has to exist
    whatever happens next; retention before the organization purge, because a tenant on
    its way out should have had its ordinary retention applied like everybody else. The
    orphan sweep is **not** here: it is report-only by design, and a destructive pass on a
    schedule is exactly what the report-first rule exists to prevent.

    Failures are logged rather than raised. arq would retry the cron entry, and a retry of
    a whole night's maintenance is not what anybody wants at 03:05 — each of these passes
    is resumable and runs again tomorrow.
    """
    platform: Platform | None = context.get("platform")
    if platform is None:  # pragma: no cover - a worker built without the platform bundle
        return
    for name, run in (
        ("partitions", platform.service.run_partitions),
        ("retention", platform.service.run_retention),
        ("organization-purge", platform.service.purge_due),
    ):
        try:
            await run()
        except Exception:
            logger.exception("scheduled maintenance failed", extra={"job": name})


async def shutdown(context: dict[str, Any]) -> None:
    settings_service = context.get("platform_settings")
    if settings_service is not None:
        await settings_service.stop()
    ingestion: Ingestion | None = context.get("ingestion")
    if ingestion is not None:
        # The extraction subprocesses are children of this process. A worker that exits
        # without stopping them leaves them behind on every restart.
        await ingestion.aclose()
    backends: VectorBackends | None = context.get("vector_backends")
    if backends is not None:
        await backends.aclose()
    clients: Clients | None = context.get("clients")
    if clients is not None:
        await clients.aclose()
    shutdown_tracing(context.get("tracing"))
    logger.info("worker stopped")


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


class WorkerSettings:
    """arq reads this class by name. Attributes only — arq instantiates nothing."""

    # `ClassVar` throughout: arq reads these off the class and never instantiates it, so
    # a mutable default here is a shared constant rather than the usual trap.
    functions: ClassVar[list[Any]] = [run_gateway_job]
    #: Task 17's scheduled work. One entry, at 03:05 UTC — a fixed hour rather than a
    #: spread one, because the passes it runs are the cheap kind and the expensive thing
    #: they replace is a table nobody pruned. arq runs a cron function on exactly one
    #: worker, which is what keeps two replicas from both dropping the same partition;
    #: the passes are idempotent anyway, and both properties are worth having.
    cron_jobs: ClassVar[list[Any]] = [
        # `cast` because arq types a cron function as taking its context positionally
        # under a protocol that also demands `**kwargs`; the shape it actually calls is
        # the one written above.
        cron(cast("Any", nightly_maintenance), hour={3}, minute={5}, run_at_startup=False)
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _redis_settings()
    #: Retries are ours. Leaving this at arq's default would produce two backoff
    #: schedules, two attempt counters, and a dead-letter record written after the queue
    #: had already given up on its own terms.
    max_tries = 1
    max_jobs = get_settings().worker_max_jobs
    #: A ceiling well above the per-file extraction cap, so the only thing that ever hits
    #: it is a job that is genuinely wedged rather than one that is merely slow.
    job_timeout = 900
    #: Results are not read by anything — the document row is the result — so they are
    #: kept only long enough to be useful when someone is watching a queue by hand.
    keep_result = 300


class HeavyWorkerSettings(WorkerSettings):
    """The same worker, reading the other queue: ``arq app.workers.main.HeavyWorkerSettings``.

    PDFs and Office documents are enqueued here (see
    :func:`~app.services.jobs.queue_for`), and everything else stays on the default queue.
    The split is about *ordering*, not capability: without it a folder of notes dropped
    alongside a 300-page manual sits at ``pending`` behind it for no reason a customer can
    see. It is also where the memory ceiling actually gets set, because concurrency here
    multiplies a parser's footprint rather than an HTTP client's — hence the lower job
    count, deliberately not derived from ``worker_max_jobs``.

    Running it is optional. With no heavy worker deployed, those jobs simply wait, which
    is a visible backlog rather than a silent loss — and a single-worker deployment can
    read both by starting two processes from the same image.
    """

    queue_name = ARQ_HEAVY_QUEUE_KEY
    max_jobs = 2


# `arq` also accepts a module-level `functions` list; naming the class explicitly in the
# command keeps the settings and the functions in one place.
__all__ = [
    "ARQ_FUNCTION",
    "HeavyWorkerSettings",
    "WorkerSettings",
    "nightly_maintenance",
    "run_gateway_job",
]
