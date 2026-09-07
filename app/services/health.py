"""Readiness probes for the four backing services.

Each probe is bounded by a timeout: a readiness endpoint that can hang is worse than one
that reports failure, because Kubernetes learns nothing from a request that never returns.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from sqlalchemy import text

from app.core.clients import Clients

logger = logging.getLogger(__name__)

DependencyStatus = Literal["ok", "error", "timeout"]


async def check_postgres(clients: Clients) -> None:
    async with clients.engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def check_redis(clients: Clients) -> None:
    await clients.redis.ping()


async def check_qdrant(clients: Clients) -> None:
    await clients.qdrant.get_collections()


async def check_storage(clients: Clients) -> None:
    # boto3 is synchronous; run it off the event loop so a slow bucket cannot block
    # every other request on this worker.
    await asyncio.to_thread(clients.storage.head_bucket, Bucket=clients.bucket)


PROBES: dict[str, Callable[[Clients], Awaitable[None]]] = {
    "postgres": check_postgres,
    "redis": check_redis,
    "qdrant": check_qdrant,
    "storage": check_storage,
}


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
    timeout_seconds: float = 2.0,
) -> dict[str, dict[str, Any]]:
    """Run every probe concurrently and report each dependency by name."""
    results = await asyncio.gather(
        *(_run_probe(name, probe, clients, timeout_seconds) for name, probe in PROBES.items())
    )
    return dict(results)


def is_ready(results: dict[str, dict[str, Any]]) -> bool:
    return all(result["status"] == "ok" for result in results.values())


def _summarize(exc: Exception) -> str:
    """A one-line cause. Probe failures carry connection strings in their text, so the
    message is truncated rather than returned whole."""
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return message[:200]
