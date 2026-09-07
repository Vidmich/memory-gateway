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
from typing import Any, ClassVar

from arq.connections import RedisSettings

from app.core.clients import Clients
from app.core.config import Settings, get_settings
from app.core.logging import bind_request_id, configure_logging
from app.core.metrics import build_metrics
from app.services.job_queue import ARQ_FUNCTION, ArqJobQueue, from_payload
from app.services.jobs import JobRunner
from app.workers.runtime import build_dead_letters, build_ingestion, build_runner

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
    clients = Clients.create(settings)
    metrics = build_metrics(
        service_name=f"{settings.service_name}-worker", version=settings.version
    )
    # arq hands the worker its own pool; reusing it means the queue the runner re-enqueues
    # retries onto is the same queue this worker is reading from.
    queue = ArqJobQueue(context["redis"])
    ingestion = build_ingestion(clients, settings, queue=queue)

    context["clients"] = clients
    context["runner"] = build_runner(
        ingestion,
        settings,
        dead_letters=build_dead_letters(clients),
        metrics=metrics.jobs,
    )
    logger.info("worker started", extra={"environment": settings.environment})


async def shutdown(context: dict[str, Any]) -> None:
    clients: Clients | None = context.get("clients")
    if clients is not None:
        await clients.aclose()
    logger.info("worker stopped")


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


class WorkerSettings:
    """arq reads this class by name. Attributes only — arq instantiates nothing."""

    # `ClassVar` throughout: arq reads these off the class and never instantiates it, so
    # a mutable default here is a shared constant rather than the usual trap.
    functions: ClassVar[list[Any]] = [run_gateway_job]
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


# `arq` also accepts a module-level `functions` list; naming the class explicitly in the
# command keeps the settings and the functions in one place.
__all__ = ["ARQ_FUNCTION", "WorkerSettings", "run_gateway_job"]
