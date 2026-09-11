"""Background work: the queue port, the retry policy, and the runner that applies it.

This is the first task with a worker, so the shape is set here for tasks 13 and 17 to
inherit. Four decisions are worth stating.

**Retries are ours, not the queue's.** ``arq`` can retry a job itself, and this build tells
it not to (``max_tries=1``). Backoff, the attempt ceiling, the classification of what is
worth retrying, and the dead-letter record are one policy that belongs in one place — and
in a place that can be tested exhaustively without Redis, which :class:`RetryPolicy` is
and a queue's internal behaviour is not. The cost is real and worth naming: a job whose
worker is killed mid-run is not automatically re-delivered. For ingestion that is the
right trade, because reconciliation already exists — the next **Resync** finds the
document still in a non-terminal state and redoes it — and a queue-level redelivery would
be a second, subtly different recovery path for the same problem.

**Idempotency is a key, not a lock.** Every enqueue carries one, the queue refuses a
duplicate while an identical job is pending, and — because that guarantee expires and
races — every job is *also* written to be safe to run twice. The key removes the common
case; the job body removes the rest. Relying on only the first is how "ingesting the same
file twice concurrently" ends up with two documents.

**Enqueue happens after commit.** :class:`JobOutbox` holds requests until the transaction
that justified them has landed. A job enqueued inside a transaction that then rolls back
references a row that does not exist, and the worker's failure is entirely mysterious
because the evidence was never written.

**The request id travels.** A control-plane request that enqueues three jobs is joined to
the log lines those jobs emit, minutes later, on another process. Without it, a customer
report of "my upload did nothing" has no thread to pull.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.logging import get_request_id
from app.core.metrics import JobMetrics

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_SECONDS = 2.0
DEFAULT_CAP_SECONDS = 300.0

#: How long the queue keeps an idempotency key reserved. Long enough to cover a slow
#: ingestion, short enough that a genuinely new upload of the same file is not swallowed.
DEFAULT_KEY_TTL_SECONDS = 3600


class PermanentJobError(Exception):
    """A failure that retrying cannot fix: a malformed payload, a deleted row, an
    unsupported format. Dead-lettered on the first attempt instead of four times."""


@dataclass(frozen=True, slots=True)
class JobRequest:
    """One unit of work, as it travels."""

    name: str
    payload: dict[str, Any]
    #: What makes two enqueues the same enqueue. Derived from the work, never random —
    #: see :func:`ingest_key` and friends below.
    idempotency_key: str
    request_id: str | None = None
    attempt: int = 1
    #: Seconds to wait before the job becomes visible. Set by a retry, zero otherwise.
    delay_seconds: float = 0.0
    #: Which queue to deliver on. ``None`` is the default one. Set at the enqueue site
    #: rather than decided by the worker, because the point of a second queue is that a
    #: worker reading the first one never sees the job at all.
    queue: str | None = None

    def next_attempt(self, *, delay_seconds: float) -> JobRequest:
        return JobRequest(
            name=self.name,
            payload=self.payload,
            # A retry must not be deduplicated against the attempt that failed, and the
            # original key may still be reserved. Attempt-qualifying it keeps the reserved
            # window doing its job for genuinely new work.
            idempotency_key=f"{self.idempotency_key}:retry{self.attempt}",
            request_id=self.request_id,
            attempt=self.attempt + 1,
            delay_seconds=delay_seconds,
            queue=self.queue,
        )


class JobQueue(Protocol):
    async def enqueue(self, request: JobRequest) -> str | None:
        """Submit a job. Returns its queue id, or ``None`` when an identical job was
        already pending — which is a success, not a failure."""
        ...

    async def depth(self) -> int:
        """Jobs waiting. Exported as a gauge; a rising one means the workers are behind."""
        ...

    async def ping(self) -> None:
        """Raise if the queue is unreachable. Backs the ``/readyz`` check."""
        ...


# ---------------------------------------------------------------------------
# retry policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Retry:
    delay_seconds: float


@dataclass(frozen=True, slots=True)
class DeadLetter:
    reason: str


Decision = Retry | DeadLetter


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with full jitter, and a ceiling on both.

    Jitter matters more here than it looks. A provider outage fails every in-flight job at
    once; a deterministic backoff sends all of them back at the same instant, and the
    second wave is exactly as synchronised as the first.
    """

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_seconds: float = DEFAULT_BASE_SECONDS
    cap_seconds: float = DEFAULT_CAP_SECONDS
    #: Injected so a test can assert on the schedule rather than on a random draw.
    jitter: Callable[[float, float], float] = random.uniform

    def delay_for(self, attempt: int) -> float:
        ceiling = min(self.base_seconds * (2 ** max(0, attempt - 1)), self.cap_seconds)
        return self.jitter(0.0, ceiling)

    def decide(self, *, attempt: int, error: BaseException) -> Decision:
        if isinstance(error, PermanentJobError):
            return DeadLetter(str(error))
        if attempt >= self.max_attempts:
            return DeadLetter(f"failed after {attempt} attempts: {_summarize(error)}")
        return Retry(self.delay_for(attempt))


def _summarize(error: BaseException) -> str:
    text = str(error).strip() or error.__class__.__name__
    return text.splitlines()[0][:500]


# ---------------------------------------------------------------------------
# dead letters
# ---------------------------------------------------------------------------


class DeadLetterSink(Protocol):
    async def record(self, request: JobRequest, *, reason: str) -> None: ...


@dataclass
class MemoryDeadLetters:
    records: list[tuple[JobRequest, str]] = field(default_factory=list)

    async def record(self, request: JobRequest, *, reason: str) -> None:
        self.records.append((request, reason))


# ---------------------------------------------------------------------------
# outbox
# ---------------------------------------------------------------------------


@dataclass
class JobOutbox:
    """Jobs held until the transaction that justified them has committed.

    Deliberately not clever. There is no database-backed outbox table here, because that
    trades one failure mode (a commit followed by a process death before the enqueue) for
    a permanent second write on every request. The window is small, and the thing that
    closes it — resync reconciling whatever the queue missed — has to exist anyway.
    """

    queue: JobQueue
    pending: list[JobRequest] = field(default_factory=list)

    def add(
        self,
        name: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
        request_id: str | None = None,
        queue: str | None = None,
    ) -> None:
        self.pending.append(
            JobRequest(
                name=name,
                payload=dict(payload),
                idempotency_key=idempotency_key,
                request_id=request_id if request_id is not None else get_request_id(),
                queue=queue,
            )
        )

    async def flush(self) -> int:
        """Enqueue everything held. Called *after* the transaction closes.

        A queue that is down must not undo work that has already committed, so a failure
        here is logged and swallowed: the document row exists and says ``pending``, and a
        resync will pick it up. Raising would return a 500 for an upload whose bytes are
        safely stored.
        """
        submitted = 0
        while self.pending:
            request = self.pending.pop(0)
            try:
                await self.queue.enqueue(request)
                submitted += 1
            except Exception:
                logger.error(
                    "could not enqueue job; a resync will recover it",
                    extra={"job": request.name, "idempotency_key": request.idempotency_key},
                    exc_info=True,
                )
        return submitted

    def discard(self) -> None:
        self.pending.clear()


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

Handler = Callable[[Mapping[str, Any]], Awaitable[None]]


class JobRunner:
    """Runs one job and applies the policy to whatever happens.

    The whole retry envelope — timing, logging, classification, re-enqueue, dead letter —
    is here rather than in each job, so a job body is only the work.
    """

    def __init__(
        self,
        handlers: Mapping[str, Handler],
        *,
        queue: JobQueue,
        dead_letters: DeadLetterSink,
        policy: RetryPolicy | None = None,
        metrics: JobMetrics | None = None,
    ) -> None:
        self._handlers = dict(handlers)
        self._queue = queue
        self._dead_letters = dead_letters
        self._policy = policy or RetryPolicy()
        self._metrics = metrics

    @property
    def names(self) -> Sequence[str]:
        return tuple(self._handlers)

    async def run(self, request: JobRequest) -> Decision | None:
        """Execute a job. Returns ``None`` on success, otherwise what was decided."""
        handler = self._handlers.get(request.name)
        if handler is None:
            # A payload for a job this build does not have. Retrying cannot help, and
            # silently dropping it would lose the evidence of a bad deploy.
            await self._bury(request, f"no handler registered for job {request.name!r}")
            return DeadLetter("unknown job")

        extra = {
            "job": request.name,
            "attempt": request.attempt,
            "idempotency_key": request.idempotency_key,
            "request_id": request.request_id,
        }
        self._count(self._metrics.started if self._metrics else None, job=request.name)
        started = time.perf_counter()
        try:
            await handler(request.payload)
        except Exception as error:
            elapsed = time.perf_counter() - started
            self._observe(request.name, elapsed)
            self._count(
                self._metrics.completed if self._metrics else None,
                job=request.name,
                outcome="failed",
            )
            decision = self._policy.decide(attempt=request.attempt, error=error)
            logger.warning("job failed", extra={**extra, "error": _summarize(error)}, exc_info=True)
            if isinstance(decision, Retry):
                later = request.next_attempt(delay_seconds=decision.delay_seconds)
                await self._queue.enqueue(later)
            else:
                await self._bury(request, decision.reason)
            return decision

        elapsed = time.perf_counter() - started
        self._observe(request.name, elapsed)
        self._count(
            self._metrics.completed if self._metrics else None,
            job=request.name,
            outcome="succeeded",
        )
        logger.info("job finished", extra={**extra, "duration_ms": int(elapsed * 1000)})
        return None

    async def _bury(self, request: JobRequest, reason: str) -> None:
        self._count(self._metrics.dead_lettered if self._metrics else None, job=request.name)
        logger.error(
            "job dead-lettered",
            extra={
                "job": request.name,
                "attempt": request.attempt,
                "idempotency_key": request.idempotency_key,
                "request_id": request.request_id,
                "reason": reason,
            },
        )
        try:
            await self._dead_letters.record(request, reason=reason)
        except Exception:
            # The log line above is the durable-enough record if the database is the
            # thing that is broken.
            logger.error("could not write dead letter", exc_info=True)

    def _observe(self, name: str, seconds: float) -> None:
        if self._metrics is not None:
            self._metrics.duration.labels(job=name).observe(seconds)

    @staticmethod
    def _count(counter: Any, **labels: str) -> None:
        if counter is not None:
            counter.labels(**labels).inc()


# ---------------------------------------------------------------------------
# job names and keys
# ---------------------------------------------------------------------------

INGEST_DOCUMENT = "ingest_document"
DELETE_CONNECTOR = "delete_connector"
#: Task 102. Just the summary of an already-indexed document: the **Summarize** retry, the
#: morning after a cap hit, the re-embed after an operator edits one.
SUMMARIZE_DOCUMENT = "summarize_document"
DISTIL_MEMORY = "distil_memory"
REINDEX = "reindex"
#: Task 19. Two, not one, because the second runs *after* a grace period: the drop of a
#: migrated-from collection is deferred so that replicas holding a cached binding are not
#: still reading it. Splitting them is what lets the delay live in the queue rather than in
#: a worker holding a slot open for a quarter of an hour.
MIGRATE_VECTORS = "migrate_vectors"
DROP_MIGRATION_SOURCE = "drop_migration_source"
#: Task 103. A connector-wide audit of the index — a scroll of the whole collection, a
#: minute for a hundred thousand points — and an evaluation run, one embedding call per
#: question. Both are things a person presses a button for and neither is a request.
AUDIT_INDEX = "audit_index"
EVALUATE_SET = "evaluate_set"

#: Every job this build knows how to run: one file's extraction and embedding, a
#: connector's whole teardown, one conversation's distillation, task 17's reindex, and
#: task 19's backend migration and its deferred cleanup — all unbounded work that a
#: request must not wait on. Resync is not here: SPEC §9.1 has
#: it return a summary, which a job cannot do, and what it does synchronously is a listing
#: plus row writes. The *scheduled* jobs are not here either: retention, partitions and the
#: orphan sweep are cron entries on the worker rather than enqueued work, because nothing
#: requests them and there is nothing to deduplicate them against.
JOB_NAMES = (
    INGEST_DOCUMENT,
    DELETE_CONNECTOR,
    SUMMARIZE_DOCUMENT,
    DISTIL_MEMORY,
    REINDEX,
    MIGRATE_VECTORS,
    DROP_MIGRATION_SOURCE,
    AUDIT_INDEX,
    EVALUATE_SET,
)


def ingest_key(document_id: uuid.UUID, content_hash: str | None) -> str:
    """One ingestion per document *version*.

    The hash is in the key on purpose. Keying on the document alone would make a re-upload
    of a corrected file a duplicate of the ingestion still running for the old one, and
    the fix would silently never be indexed.
    """
    return f"ingest:{document_id}:{content_hash or 'unknown'}"


def audit_key(audit_id: uuid.UUID) -> str:
    """One job per audit row. The row is created first, so a second click while the first
    is running finds the running row and never reaches the queue."""
    return f"audit:{audit_id}"


def evaluate_key(run_id: uuid.UUID) -> str:
    """One job per run row, for the same reason as :func:`audit_key`."""
    return f"evaluate:{run_id}"


def summarize_key(document_id: uuid.UUID, content_hash: str | None) -> str:
    """One summarization per document *version*, like :func:`ingest_key`. The callers
    that mean "again, even though nothing changed" qualify it."""
    return f"summarize:{document_id}:{content_hash or 'unknown'}"


#: The one non-default queue. A *logical* name: turning it into a Redis key is the
#: adapter's business, and :class:`MemoryJobQueue` has no keys at all.
HEAVY_QUEUE = "heavy"

#: Extensions whose extraction is slow enough to be worth a queue of its own. Chosen from
#: the *name* rather than the sniffed type because this is a scheduling decision made
#: before the bytes have been read, and being wrong about one costs nothing but ordering.
HEAVY_EXTENSIONS = frozenset({".pdf", ".docx", ".pptx", ".xlsx"})


def queue_for(source_name: str) -> str | None:
    """Which queue a document's ingestion belongs on.

    A 300-page PDF takes seconds to read; a Markdown file takes a millisecond. On one
    queue the second waits behind the first, and a customer who dropped a folder of notes
    alongside a manual watches the notes sit at ``pending`` for no reason they can see.
    Two queues and two worker deployments make that a capacity decision instead — and the
    heavy one can be given fewer concurrent jobs, which is what actually bounds memory
    when every job holds a parser.
    """
    from app.services.filetypes import extension_of

    return HEAVY_QUEUE if extension_of(source_name) in HEAVY_EXTENSIONS else None


def distil_key(end_user_id: uuid.UUID, session_id: str | None, token: str) -> str:
    """One key per *armed pass*, not per conversation.

    The token is in the key on purpose, and it is the opposite of what
    :func:`ingest_key` does. Two turns of one conversation must produce two jobs, because
    the second one exists precisely to supersede the first: deduplicating them would leave
    the pass scheduled at the first turn's deadline, in the middle of the exchange it is
    supposed to be waiting out. What stops the work from happening twice is the debounce
    token, checked when the job runs — not the queue.
    """
    return f"distil:{end_user_id}:{session_id or '-'}:{token}"


def reindex_key(run_id: uuid.UUID) -> str:
    """One job per run row.

    The run is created before the enqueue and carries its own progress, so a duplicate
    enqueue is the *same* work rather than a second copy of it — and the handler is safe
    to run twice anyway, because every target resumes from its cursor.
    """
    return f"reindex:{run_id}"


def resync_key(connector_id: uuid.UUID) -> str:
    """The lock name for a reconciliation. Pressing the button twice is one sync."""
    return f"resync:{connector_id}"


def delete_key(connector_id: uuid.UUID) -> str:
    return f"delete:{connector_id}"


__all__ = [
    "AUDIT_INDEX",
    "DELETE_CONNECTOR",
    "DISTIL_MEMORY",
    "DROP_MIGRATION_SOURCE",
    "EVALUATE_SET",
    "HEAVY_EXTENSIONS",
    "HEAVY_QUEUE",
    "INGEST_DOCUMENT",
    "JOB_NAMES",
    "MIGRATE_VECTORS",
    "REINDEX",
    "DeadLetter",
    "DeadLetterSink",
    "Decision",
    "JobOutbox",
    "JobQueue",
    "JobRequest",
    "JobRunner",
    "MemoryDeadLetters",
    "PermanentJobError",
    "Retry",
    "RetryPolicy",
    "audit_key",
    "delete_key",
    "distil_key",
    "evaluate_key",
    "ingest_key",
    "queue_for",
    "reindex_key",
    "resync_key",
]
