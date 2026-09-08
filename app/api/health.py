"""Liveness, readiness, and metrics endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.clients import Clients
from app.core.config import Settings
from app.core.lifecycle import Lifecycle
from app.core.metrics import Metrics
from app.services.health import is_ready, run_readiness_checks
from app.workers.runtime import Ingestion

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"], include_in_schema=False)


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    """Liveness: process is up. Deliberately checks nothing else — a dependency outage
    must not get the pod killed and restarted into the same outage."""
    settings: Settings = request.app.state.settings
    return {"status": "ok", "version": settings.version}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, Any]:
    """Readiness: this instance can serve traffic right now.

    A draining pod answers 503 here *before* it stops accepting, which is the whole
    mechanism in :mod:`app.core.lifecycle`: the load balancer watches this endpoint, and
    the seconds between a ``SIGTERM`` and the removal of the pod's endpoint are seconds in
    which new requests are still routed here. Answered without probing anything, because
    the answer does not depend on Postgres being up and because a shutdown is not the
    moment to add four network calls to a probe that runs every couple of seconds.
    """
    settings: Settings = request.app.state.settings
    clients: Clients = request.app.state.clients

    lifecycle: Lifecycle | None = getattr(request.app.state, "lifecycle", None)
    if lifecycle is not None and lifecycle.draining:
        response.status_code = 503
        return {"status": "draining"}

    results = await run_readiness_checks(
        clients, timeout_seconds=settings.readiness_timeout_seconds
    )
    body: dict[str, Any] = {name: result["status"] for name, result in results.items()}

    if not is_ready(results):
        response.status_code = 503
        # Name what is broken; a bare 503 sends the on-call engineer to the wrong place.
        body["detail"] = {
            name: result["detail"] for name, result in results.items() if result["status"] != "ok"
        }

    return body


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    registry: Metrics = request.app.state.metrics
    # Sampled at scrape time rather than maintained on every enqueue: the depth is a
    # property of the queue, not of this process, and a gauge each replica updated from
    # its own enqueues would report a different number on every one of them.
    ingestion: Ingestion | None = getattr(request.app.state, "ingestion", None)
    if ingestion is not None:
        try:
            registry.jobs.queue_depth.set(await ingestion.queue.depth())
        except Exception:
            # A scrape must not fail because Redis is briefly away; the other metrics are
            # what tell somebody it is.
            logger.warning("could not read job queue depth", exc_info=True)
    return Response(
        content=generate_latest(registry.registry),
        media_type=CONTENT_TYPE_LATEST,
    )
