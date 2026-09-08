"""Queue adapters: ``arq`` over Redis, and an in-process one.

``arq`` is used for exactly two things — durable delivery and deferral — and nothing else.
Retries, backoff, attempt counting and dead-lettering are :mod:`app.services.jobs`, for
the reasons that module's docstring gives. That is why every job goes through a single arq
function, :data:`ARQ_FUNCTION`, carrying a serialised :class:`~app.services.jobs.JobRequest`:
one function means arq has no opinion about the work, and the shape of a job is ours to
change without a queue migration.

Idempotency uses arq's ``_job_id``. A second enqueue under a key that is still pending is
refused by Redis and returns ``None``, which the caller treats as success — the work is
already scheduled, and that is what was asked for.

:class:`MemoryJobQueue` is not only for tests. It runs the whole pipeline in-process,
which is what makes a data-plane or API test able to assert on an *indexed* document
without a worker, and what the ``inline`` mode uses for a single-process development run.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.services.jobs import HEAVY_QUEUE, JobRequest, JobRunner

logger = logging.getLogger(__name__)

#: The one arq function every job is delivered through.
ARQ_FUNCTION = "run_gateway_job"

#: arq's default sorted-set key. Read for the depth gauge and the readiness probe; named
#: here rather than inlined so a queue rename is one edit.
ARQ_QUEUE_KEY = "arq:queue"

#: The heavy queue's key. Its own sorted set, which is the whole point: a worker started
#: with ``queue_name`` set to this one never sees an ordinary Markdown file, and a
#: 300-page PDF never sits in front of one.
ARQ_HEAVY_QUEUE_KEY = "arq:queue:heavy"

#: Logical queue name to Redis key. The mapping lives here because it is an arq detail;
#: :mod:`app.services.jobs` names queues without knowing they are sorted sets.
QUEUE_KEYS: dict[str | None, str] = {None: ARQ_QUEUE_KEY, HEAVY_QUEUE: ARQ_HEAVY_QUEUE_KEY}


def as_payload(request: JobRequest) -> dict[str, Any]:
    """The wire form. Plain JSON-able types only: this crosses a process boundary and
    survives a deploy, so anything richer would be a version dependency between the API
    and the worker."""
    return {
        "name": request.name,
        "payload": request.payload,
        "idempotency_key": request.idempotency_key,
        "request_id": request.request_id,
        "attempt": request.attempt,
        # Carried so a retry lands back on the queue the work belongs to. Without it the
        # second attempt at a 300-page PDF would be scheduled in front of the light work
        # the split exists to protect.
        "queue": request.queue,
    }


def from_payload(data: dict[str, Any]) -> JobRequest:
    return JobRequest(
        name=str(data["name"]),
        payload=dict(data.get("payload") or {}),
        idempotency_key=str(data.get("idempotency_key") or ""),
        request_id=data.get("request_id"),
        attempt=int(data.get("attempt") or 1),
        queue=data.get("queue") or None,
    )


def serialize(value: dict[str, Any]) -> bytes:
    """arq's default job serializer is pickle. This is not.

    A payload here is three strings, so JSON costs nothing — and it removes the class of
    problem where anything able to write to Redis can execute code in a worker. It also
    means a payload written by one build and read by another is a data question rather
    than a Python-version question.
    """
    return json.dumps(value, default=str).encode("utf-8")


def deserialize(raw: bytes) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(raw)
    return loaded


def create_job_pool(redis_url: str) -> Any:
    """An arq client over Redis, without connecting.

    Built from a lazy :class:`ConnectionPool` rather than with ``arq.create_pool``, which
    connects and pings. That matters here: :meth:`Clients.create` opens no sockets, the
    application factory is called in tests with nothing running, and a connect at
    construction would turn "build the app" into "the stack must be up".
    """
    from arq.connections import ArqRedis
    from redis.asyncio import ConnectionPool

    return ArqRedis(
        ConnectionPool.from_url(redis_url),
        job_serializer=serialize,
        job_deserializer=deserialize,
    )


class ArqJobQueue:
    """Redis-backed delivery."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def enqueue(self, request: JobRequest) -> str | None:
        job = await self._pool.enqueue_job(
            ARQ_FUNCTION,
            as_payload(request),
            _job_id=request.idempotency_key,
            _queue_name=QUEUE_KEYS.get(request.queue, ARQ_QUEUE_KEY),
            _defer_by=(
                timedelta(seconds=request.delay_seconds) if request.delay_seconds > 0 else None
            ),
        )
        if job is None:
            logger.info(
                "job already queued; not enqueuing a duplicate",
                extra={"job": request.name, "idempotency_key": request.idempotency_key},
            )
            return None
        return str(job.job_id)

    async def depth(self) -> int:
        """Every queue, added together.

        The gauge answers "is ingestion falling behind", and that question is about the
        backlog rather than about which sorted set it is sitting in. Splitting it by queue
        would be two series that have to be summed at read time to get the number anyone
        actually alerts on.
        """
        total = 0
        for key in set(QUEUE_KEYS.values()):
            total += int(await self._pool.zcard(key))
        return total

    async def ping(self) -> None:
        await self._pool.ping()


@dataclass
class MemoryJobQueue:
    """The same queue in a list.

    Deduplication mirrors arq's: a key that is still pending refuses the second enqueue.
    A key that has *run* does not, which is also arq's behaviour once the result expires,
    and is why every job body is independently safe to run twice.
    """

    pending: list[JobRequest] = field(default_factory=list)
    #: Everything ever accepted, in order. What a test asserts against when it cares that
    #: a job was requested rather than that it ran.
    submitted: list[JobRequest] = field(default_factory=list)
    reachable: bool = True

    async def enqueue(self, request: JobRequest) -> str | None:
        if any(job.idempotency_key == request.idempotency_key for job in self.pending):
            return None
        self.pending.append(request)
        self.submitted.append(request)
        return request.idempotency_key

    async def depth(self) -> int:
        return len(self.pending)

    async def ping(self) -> None:
        if not self.reachable:
            raise ConnectionError("queue is unreachable")

    async def drain(self, runner: JobRunner, *, limit: int = 200) -> int:
        """Run everything queued, including whatever those jobs enqueue.

        Bounded, because a job that re-enqueues itself on every failure would otherwise
        spin here forever and the test would hang rather than fail.
        """
        done = 0
        while self.pending and done < limit:
            request = self.pending.pop(0)
            await runner.run(request)
            done += 1
        if self.pending:
            raise AssertionError(f"queue still had {len(self.pending)} jobs after {limit} runs")
        return done

    def names(self) -> Sequence[str]:
        return [job.name for job in self.submitted]


__all__ = [
    "ARQ_FUNCTION",
    "ARQ_HEAVY_QUEUE_KEY",
    "ARQ_QUEUE_KEY",
    "QUEUE_KEYS",
    "ArqJobQueue",
    "MemoryJobQueue",
    "as_payload",
    "from_payload",
]
