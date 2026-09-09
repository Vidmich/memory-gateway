"""Readiness probes for the backing services.

Each probe is bounded by a timeout: a readiness endpoint that can hang is worse than one
that reports failure, because Kubernetes learns nothing from a request that never returns.

**Vector backends are reported individually and judged together.** Since task 19 a
deployment can have more than one, with different organizations on each, and the rule is
:func:`is_ready`: every other dependency must be healthy, and *at least one* vector backend
must be. The reasoning is what readiness is for — "should this pod receive traffic". If one
backend is down, this pod is not the thing that is broken; taking it out of rotation
removes capacity from the tenants who are still fine and helps the affected ones not at
all. With a single backend configured the rule is exactly what it was before: Qdrant down
means not ready.

What *does* protect the affected tenants is per-organization: their gateway's ``fail_open``
setting decides whether a request proceeds without retrieval, and the alert fires on the
per-backend status reported here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from sqlalchemy import text

from app.core.clients import Clients
from app.services.vector_backends import Backend, VectorBackends

logger = logging.getLogger(__name__)

DependencyStatus = Literal["ok", "error", "timeout"]


async def check_postgres(clients: Clients) -> None:
    async with clients.engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def check_redis(clients: Clients) -> None:
    await clients.redis.ping()


async def check_jobs(clients: Clients) -> None:
    """The job queue, checked separately from Redis even though it *is* Redis.

    They fail for different reasons and mean different things: a Redis outage takes the
    whole service down, while a queue that cannot be written to leaves the API serving
    traffic and silently dropping ingestion. Reporting the second as the first would send
    whoever is on call to look at the wrong thing.
    """
    await clients.jobs.ping()


async def check_storage(clients: Clients) -> None:
    # boto3 is synchronous; run it off the event loop so a slow bucket cannot block
    # every other request on this worker.
    await asyncio.to_thread(clients.storage.head_bucket, Bucket=clients.bucket)


PROBES: dict[str, Callable[[Clients], Awaitable[None]]] = {
    "postgres": check_postgres,
    "redis": check_redis,
    "storage": check_storage,
    "jobs": check_jobs,
}

#: Prefix for a vector backend's entry in the readiness payload. Named rather than
#: interpolated at three sites, because :func:`is_ready` recognises these entries by it
#: and a typo would quietly reclassify a backend as a hard dependency.
VECTOR_PREFIX = "vectors."


async def _run_probe(
    name: str,
    probe: Callable[[Clients], Awaitable[None]],
    clients: Clients,
    timeout_seconds: float,
) -> tuple[str, dict[str, Any]]:
    try:
        await asyncio.wait_for(probe(clients), timeout=timeout_seconds)
    except TimeoutError:
        logger.warning("readiness probe timed out", extra={"dependency": name})
        return name, {"status": "timeout", "detail": f"no response within {timeout_seconds}s"}
    except Exception as exc:
        logger.warning(
            "readiness probe failed",
            extra={"dependency": name, "error": str(exc)},
        )
        return name, {"status": "error", "detail": _summarize(exc)}
    return name, {"status": "ok"}


async def run_readiness_checks(
    clients: Clients,
    *,
    backends: VectorBackends | None = None,
    timeout_seconds: float = 2.0,
) -> dict[str, dict[str, Any]]:
    """Run every probe concurrently and report each dependency by name."""
    probes: dict[str, Callable[[Clients], Awaitable[None]]] = dict(PROBES)
    for backend in backends.every() if backends is not None else ():
        probes[f"{VECTOR_PREFIX}{backend.kind}"] = _vector_probe(backend)
    results = await asyncio.gather(
        *(_run_probe(name, probe, clients, timeout_seconds) for name, probe in probes.items())
    )
    return dict(results)


def _vector_probe(backend: Backend) -> Callable[[Clients], Awaitable[None]]:
    async def probe(_: Clients) -> None:
        if backend.ping is None:
            return
        await backend.ping()

    return probe


def is_ready(results: dict[str, dict[str, Any]]) -> bool:
    """Every non-vector dependency healthy, and at least one vector backend.

    See the module docstring for why the vector rule is a disjunction. Note that it
    degrades to the old behaviour exactly: with one backend configured, "at least one of
    one" is "that one".
    """
    vectors = {name: result for name, result in results.items() if name.startswith(VECTOR_PREFIX)}
    others = {name: result for name, result in results.items() if name not in vectors}
    if not all(result["status"] == "ok" for result in others.values()):
        return False
    if not vectors:
        return True
    return any(result["status"] == "ok" for result in vectors.values())


def _summarize(exc: Exception) -> str:
    """A one-line cause. Probe failures carry connection strings in their text, so the
    message is truncated rather than returned whole."""
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return message[:200]
