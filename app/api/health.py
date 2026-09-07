"""Liveness, readiness, and metrics endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.clients import Clients
from app.core.config import Settings
from app.core.metrics import Metrics
from app.services.health import is_ready, run_readiness_checks

router = APIRouter(tags=["health"], include_in_schema=False)


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    """Liveness: process is up. Deliberately checks nothing else — a dependency outage
    must not get the pod killed and restarted into the same outage."""
    settings: Settings = request.app.state.settings
    return {"status": "ok", "version": settings.version}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, Any]:
    """Readiness: this instance can serve traffic right now."""
    settings: Settings = request.app.state.settings
    clients: Clients = request.app.state.clients

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
    return Response(
        content=generate_latest(registry.registry),
        media_type=CONTENT_TYPE_LATEST,
    )
