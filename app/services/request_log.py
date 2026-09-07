"""Recording what happened, without making it happen more slowly.

SPEC §10.2 wants a row for every request, with bodies. Task 07's acceptance criterion
puts a number on the cost: **under 5 ms added at p95**. Those two together rule out the
obvious implementation — an ``INSERT`` before the response returns — because a database
round trip is the same order of magnitude as the whole budget, and a database that is
briefly slow would then make the *proxy* briefly slow. Logging must never be able to take
the data plane down, and the only way to guarantee that is for the data plane never to
wait on it.

So the request path does three things and none of them is I/O: it fills in a mutable
:class:`RequestRecord` as the request progresses, it copies at most a bounded number of
bytes out of the response, and it calls :meth:`LogSink.submit`, which is a bounded
in-memory queue and returns immediately. Everything else — redaction, batching, the
insert — happens in :class:`LogFlusher`, a background task.

Four properties are worth stating because they are the ones that break under load.

**The queue is bounded and the drop policy is ordered.** Under pressure, bodies go first
and whole records go second, because a metadata row with no transcript still answers "how
many requests, how fast, how many failed" and a missing row answers nothing. Every drop
is counted, so a gap in the logs is visible as a number rather than inferred from a
suspicion.

**Toggles are applied at collection, redaction at flush.** If a gateway has
``log_response_body`` off, nothing is copied out of the stream at all — the cheapest
possible implementation of "off". Redaction is the opposite: it is regular expressions
over customer text, it is the one part with unbounded cost, and it runs in the flusher
where a slow pattern costs throughput rather than latency. It still happens strictly
before the row is written, which is what SPEC §10.2 requires.

**A stream is teed, not buffered.** Frames are relayed downstream the instant they
arrive; the tee copies the text deltas into a capped buffer on the way past. Past the cap
it stops copying and sets a truncation marker, so a runaway generation costs a marker
rather than the process.

**Nothing here raises into the request path.** :meth:`LogSink.submit` cannot fail, and
every collection method is written so that a malformed body or a surprising provider
response loses the log line rather than the request.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from app.adapters.base import UpstreamTarget
from app.core.errors import AppError
from app.core.ids import uuid7
from app.core.metrics import LogMetrics
from app.schemas.gateway_config import LoggingConfig
from app.schemas.openai import ChatMessage, ChatRequest, ChatResponse, StreamFrame, Usage
from app.services.redaction import DEFAULT_BUDGET_SECONDS, Redactor

logger = logging.getLogger(__name__)

#: Records per insert batch. Chosen so a batch is one round trip at a size PostgreSQL
#: handles as a single statement without the parameter count becoming a problem.
BATCH_SIZE = 100

#: How long a partial batch waits for company. Half a second bounds how stale the live
#: tail can be; longer would make the monitoring screen feel broken during quiet periods.
FLUSH_INTERVAL_SECONDS = 0.5

#: Records held in memory before the drop policy starts firing. At ~1 KiB of metadata
#: each this is single-digit megabytes, which is the right order for a safety valve.
QUEUE_MAX_RECORDS = 10_000

#: Above this fraction of the queue, incoming records lose their bodies. Well below full,
#: because shedding the expensive half early is what keeps the queue from reaching full
#: at all — waiting until it is full means dropping records that could have been kept.
BODY_SHED_FRACTION = 0.7

#: Longest reassembled completion stored per streamed response. A response past this is a
#: runaway generation, and the prefix plus a marker is more useful than the whole of it.
MAX_RESPONSE_CHARS = 256_000


class LogSink(Protocol):
    """What the request path is allowed to know about logging: one call, no failure."""

    def submit(self, record: RequestRecord) -> None: ...


@dataclass(frozen=True, slots=True)
class LogPolicy:
    """What this gateway asked to be captured, resolved once per request.

    A frozen snapshot rather than a reference to the gateway's configuration: the
    configuration can change while a stream is open, and a request must be logged the way
    it was configured when it started, not the way it was configured when it finished.
    """

    request_body: bool = True
    assembled_prompt: bool = True
    response_body: bool = True
    redaction_patterns: tuple[str, ...] = ()

    @classmethod
    def of(cls, config: LoggingConfig) -> LogPolicy:
        return cls(
            request_body=config.log_request_body,
            assembled_prompt=config.log_assembled_prompt,
            response_body=config.log_response_body,
            redaction_patterns=tuple(config.redaction_patterns),
        )

    @property
    def wants_bodies(self) -> bool:
        return self.request_body or self.assembled_prompt or self.response_body


@dataclass(slots=True)
class RequestRecord:
    """One request, as it is known so far.

    Mutable, and mutated from exactly one coroutine — the one serving the request — so
    there is no locking here and none is needed.
    """

    organization_id: uuid.UUID
    gateway_id: uuid.UUID
    id: uuid.UUID = field(default_factory=uuid7)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    api_key_id: uuid.UUID | None = None
    end_user_id: uuid.UUID | None = None
    session_id: str | None = None
    upstream_model_id: uuid.UUID | None = None
    model_name: str | None = None

    status_code: int = 500
    error_code: str | None = None
    error_message: str | None = None
    streamed: bool = False

    latency_total_ms: int = 0
    latency_retrieval_ms: int | None = None
    latency_ttft_ms: int | None = None
    latency_upstream_ms: int | None = None

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    memory_tokens: int | None = None

    retrieved_chunk_ids: list[Any] = field(default_factory=list)
    retrieved_fact_ids: list[Any] = field(default_factory=list)
    failover_attempts: list[Any] = field(default_factory=list)

    request_id: str | None = None
    response_truncated: bool = False
    #: ``queue_pressure`` or ``redaction_budget``. Null means nothing dropped them.
    bodies_omitted: str | None = None

    request_body: list[dict[str, Any]] | None = None
    assembled_prompt: list[dict[str, Any]] | None = None
    response_body: str | None = None

    policy: LogPolicy = field(default_factory=LogPolicy)

    @property
    def has_bodies(self) -> bool:
        return (
            self.request_body is not None
            or self.assembled_prompt is not None
            or self.response_body is not None
        )

    def without_bodies(self, reason: str) -> RequestRecord:
        """A copy carrying only metadata. The row still lands; the transcript does not."""
        return replace(
            self,
            request_body=None,
            assembled_prompt=None,
            response_body=None,
            bodies_omitted=reason,
        )


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------


class RequestRecorder:
    """Fills in a :class:`RequestRecord` as the request runs, then submits it once.

    Every method is defensive: this object sits on the hot path of a request that is
    already working, and an exception raised while writing a log line would turn a
    successful completion into a 500. Anything that goes wrong here costs the log line.
    """

    def __init__(self, record: RequestRecord, sink: LogSink) -> None:
        self._record = record
        self._sink = sink
        self._started = time.perf_counter()
        self._upstream_started: float | None = None
        self._submitted = False

    @property
    def record(self) -> RequestRecord:
        return self._record

    @property
    def policy(self) -> LogPolicy:
        return self._record.policy

    def client_request(self, request: ChatRequest) -> None:
        """The caller's own messages, before any layer was prepended."""
        self._record.streamed = bool(request.stream)
        if self._record.policy.request_body:
            self._record.request_body = _messages(request.messages)

    def prepared(self, messages: Sequence[ChatMessage], target: UpstreamTarget) -> None:
        """What is about to go upstream, after assembly and the parameter merge.

        The model's *name* is copied as well as its id, because the row has to survive
        that model being deleted — see the note on foreign keys in
        :mod:`app.db.models.request_log`.
        """
        if self._record.policy.assembled_prompt:
            self._record.assembled_prompt = _messages(messages)
        self._record.upstream_model_id = target.id
        self._record.model_name = target.name

    def upstream_call_started(self) -> None:
        self._upstream_started = time.perf_counter()

    def first_token(self) -> None:
        """The first frame of a stream reached the client. Recorded once."""
        if self._record.latency_ttft_ms is None:
            self._record.latency_ttft_ms = self._elapsed_ms()

    def completed(
        self,
        *,
        status_code: int = 200,
        text: str | None = None,
        usage: Usage | None = None,
        truncated: bool = False,
    ) -> None:
        self._record.status_code = status_code
        self._record.response_truncated = truncated
        if text is not None and self._record.policy.response_body:
            self._record.response_body = text
        if usage is not None:
            self._record.prompt_tokens = usage.prompt_tokens
            self._record.completion_tokens = usage.completion_tokens
        self._close_upstream()

    def from_response(self, response: ChatResponse) -> None:
        """Pull the completion text and usage out of a non-streamed answer."""
        first = response.choices[0] if response.choices else None
        content = first.message.content if first is not None and first.message else None
        self.completed(status_code=200, text=content, usage=response.usage)

    def failed(self, error: BaseException, *, status_code: int | None = None) -> None:
        """Record a failure. Called from the route's ``except``, before re-raising."""
        if isinstance(error, AppError):
            self._record.status_code = status_code or error.status_code
            self._record.error_code = error.code
            self._record.error_message = error.message[:MAX_ERROR_CHARS]
        else:
            self._record.status_code = status_code or 500
            self._record.error_code = "internal_error"
            self._record.error_message = type(error).__name__
        self._close_upstream()

    def stream_ended_early(self, error: BaseException) -> None:
        """A stream that started successfully and did not finish."""
        self._record.error_code = (
            "client_disconnected" if isinstance(error, asyncio.CancelledError) else "stream_failed"
        )
        self._record.error_message = type(error).__name__

    def submit(self) -> None:
        """Hand the record over. Safe to call twice; the second call does nothing."""
        if self._submitted:
            return
        self._submitted = True
        self._record.latency_total_ms = self._elapsed_ms()
        try:
            self._sink.submit(self._record)
        except Exception:  # pragma: no cover - a sink that raises is a bug, not a 500
            logger.warning("could not submit a request log record", exc_info=True)

    def _close_upstream(self) -> None:
        if self._upstream_started is not None and self._record.latency_upstream_ms is None:
            self._record.latency_upstream_ms = int(
                (time.perf_counter() - self._upstream_started) * 1000
            )

    def _elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._started) * 1000)


#: Longest upstream error text kept on a log row. The full text is in the structured log.
MAX_ERROR_CHARS = 2000


class StreamTee:
    """Copies the text of a streamed completion out of frames on their way past.

    Bounded: once :data:`MAX_RESPONSE_CHARS` have been copied it stops accumulating and
    remembers that it did. The alternative — growing with the response — makes a single
    misbehaving generation an out-of-memory event on a shared process.
    """

    def __init__(self, *, limit: int = MAX_RESPONSE_CHARS, capture: bool = True) -> None:
        self._parts: list[str] = []
        self._length = 0
        self._limit = limit
        self._capture = capture
        self.truncated = False
        self.usage: Usage | None = None

    def observe(self, frame: StreamFrame) -> None:
        chunk = frame.chunk
        if chunk is None:
            return
        if chunk.usage is not None:
            # Providers send usage in a final chunk, and only when asked. Recorded when
            # it arrives; left null when it does not, because a guess in a token-count
            # chart is worse than a gap.
            self.usage = chunk.usage
        if not self._capture:
            return
        for choice in chunk.choices:
            content = choice.delta.content
            if not content:
                continue
            if self._length >= self._limit:
                self.truncated = True
                return
            room = self._limit - self._length
            self._parts.append(content[:room])
            self._length += min(len(content), room)
            if len(content) > room:
                self.truncated = True

    @property
    def text(self) -> str | None:
        """The reassembled completion, or ``None`` when nothing was captured.

        Identical to what the non-streamed path stores for the same generation: both are
        the concatenation of the assistant's content and nothing else.
        """
        return "".join(self._parts) if self._capture else None


def _messages(
    messages: Iterable[ChatMessage] | Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Messages as plain JSON, defensively.

    ``exclude_none`` so a stored body reads like the request that was sent rather than
    like a schema dump full of nulls.
    """
    result: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, ChatMessage):
            result.append(message.model_dump(exclude_none=True))
        elif isinstance(message, Mapping):
            result.append(dict(message))
    return result


# ---------------------------------------------------------------------------
# the queue
# ---------------------------------------------------------------------------


class LogWriter(Protocol):
    """Where a flushed batch goes. One call per batch, and it may raise."""

    async def write(self, records: Sequence[RequestRecord]) -> None: ...


class NullSink:
    """Drops everything. Used where a component needs a sink and the test does not care."""

    def submit(self, record: RequestRecord) -> None:
        return None


class LogQueue:
    """A bounded queue with an ordered drop policy.

    The policy is the whole point, so it is written as three cases rather than as a
    ``try/except QueueFull``:

    * below the shedding watermark — everything is kept;
    * above it — the record keeps its metadata and loses its bodies, which is where
      almost all of the memory is;
    * full — the record is dropped entirely.

    Every case increments a counter. A gap in the request log has to be a number
    somebody can alert on, not something discovered by noticing an absence.
    """

    def __init__(
        self,
        *,
        metrics: LogMetrics,
        maxsize: int = QUEUE_MAX_RECORDS,
        shed_fraction: float = BODY_SHED_FRACTION,
    ) -> None:
        self._queue: asyncio.Queue[RequestRecord] = asyncio.Queue(maxsize=maxsize)
        self._metrics = metrics
        self._maxsize = maxsize
        self._shed_at = int(maxsize * shed_fraction)

    def submit(self, record: RequestRecord) -> None:
        depth = self._queue.qsize()
        self._metrics.queue_depth.set(depth)

        if depth >= self._shed_at and record.has_bodies:
            record = record.without_bodies("queue_pressure")
            self._metrics.dropped.labels(reason="bodies").inc()

        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self._metrics.dropped.labels(reason="record").inc()

    def qsize(self) -> int:
        return self._queue.qsize()

    async def take_batch(
        self,
        *,
        max_records: int = BATCH_SIZE,
        interval_seconds: float = FLUSH_INTERVAL_SECONDS,
    ) -> list[RequestRecord]:
        """Block for the first record, then gather company for up to ``interval_seconds``.

        Blocking for the first one rather than polling is what makes an idle process
        genuinely idle. The window only starts once there is something to write, so a
        single request in a quiet minute waits half a second, not a whole one.
        """
        batch = [await self._queue.get()]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + interval_seconds

        while len(batch) < max_records:
            try:
                batch.append(self._queue.get_nowait())
                continue
            except asyncio.QueueEmpty:
                pass
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(self._queue.get(), remaining))
            except TimeoutError:
                break

        self._metrics.queue_depth.set(self._queue.qsize())
        return batch

    def drain_now(self) -> list[RequestRecord]:
        """Everything queued, without waiting. For shutdown."""
        batch: list[RequestRecord] = []
        with contextlib.suppress(asyncio.QueueEmpty):
            while True:
                batch.append(self._queue.get_nowait())
        return batch


class LogFlusher:
    """The background half: redact, batch, write, and never die.

    A write failure is logged and the batch is discarded. Retrying would be the obvious
    alternative and it is wrong here: the failure that matters is the database being
    slow or down, and a retry loop in front of a bounded queue converts that into
    unbounded memory growth and then into dropped records anyway. Losing a batch of log
    rows during a database outage is the correct trade; the counter says how many.
    """

    def __init__(
        self,
        queue: LogQueue,
        writer: LogWriter,
        *,
        metrics: LogMetrics,
        batch_size: int = BATCH_SIZE,
        interval_seconds: float = FLUSH_INTERVAL_SECONDS,
        redaction_budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    ) -> None:
        self._queue = queue
        self._writer = writer
        self._metrics = metrics
        self._batch_size = batch_size
        self._interval = interval_seconds
        self._redaction_budget = redaction_budget_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="request-log-flusher")

    async def stop(self, *, timeout_seconds: float = 5.0) -> None:
        """Stop the loop, then write whatever is still queued.

        The final drain is not optional. A rolling deploy stops processes constantly, and
        without it every deploy would silently lose up to half a second of every replica's
        traffic — which is exactly the sort of gap that gets diagnosed as a routing bug.
        """
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout_seconds)

        await self.flush_pending()

    async def flush_pending(self) -> int:
        """Write everything queued right now, and say how many rows that was.

        Public because shutdown is not the only caller that needs "make the queue empty,
        synchronously": a test that asserts on what was recorded has to be able to ask
        for it without racing a half-second timer.
        """
        batch = self._queue.drain_now()
        if batch:
            await self._write(batch)
        return len(batch)

    async def _run(self) -> None:
        while True:
            batch = await self._queue.take_batch(
                max_records=self._batch_size, interval_seconds=self._interval
            )
            await self._write(batch)

    async def _write(self, batch: Sequence[RequestRecord]) -> None:
        prepared = [self._redact(record) for record in batch]
        try:
            await self._writer.write(prepared)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._metrics.dropped.labels(reason="write_failed").inc(len(prepared))
            logger.warning(
                "could not write a batch of request logs",
                extra={"records": len(prepared)},
                exc_info=True,
            )
            return
        self._metrics.written.inc(len(prepared))

    def _redact(self, record: RequestRecord) -> RequestRecord:
        """Apply the gateway's patterns, or drop the bodies trying.

        Compiling per record rather than caching per gateway: a batch is at most a
        hundred records and the pattern lists are at most twenty short expressions, so
        the compile cache inside ``re`` makes this a dictionary lookup. Caching by
        gateway would need invalidating when the configuration changes, which is a
        correctness problem in exchange for a saving nobody can measure.
        """
        if not record.has_bodies or not record.policy.redaction_patterns:
            return record

        redactor = Redactor(record.policy.redaction_patterns, budget_seconds=self._redaction_budget)
        request_body, ok_request = redactor.scrub(record.request_body)
        assembled, ok_prompt = redactor.scrub(record.assembled_prompt)
        response, ok_response = redactor.scrub(record.response_body)

        if not (ok_request and ok_prompt and ok_response):
            # Fail closed. A partly-redacted body is the one outcome worse than no body,
            # because it looks like it was cleaned.
            logger.warning(
                "redaction did not finish within its budget; dropping the bodies",
                extra={"gateway_id": str(record.gateway_id)},
            )
            self._metrics.dropped.labels(reason="redaction").inc()
            return record.without_bodies("redaction_budget")

        return replace(
            record,
            request_body=request_body,
            assembled_prompt=assembled,
            response_body=response,
        )


class StreamRecorder:
    """Adapts a recorder and a tee onto :class:`app.services.proxy.StreamObserver`.

    It is the piece that makes a streamed request produce the *same* transcript a
    non-streamed one would: the deltas are concatenated as they pass, and the row is
    submitted from :meth:`done`, which the proxy calls exactly once however the stream
    ended. A client that hangs up mid-generation still gets a row — with the partial
    completion and ``client_disconnected`` — because that is precisely the request
    somebody will come asking about.
    """

    def __init__(self, recorder: RequestRecorder) -> None:
        self._recorder = recorder
        self._tee = StreamTee(capture=recorder.policy.response_body)
        self._first = True

    def frame(self, frame: StreamFrame) -> None:
        if self._first:
            self._first = False
            self._recorder.first_token()
        self._tee.observe(frame)

    def done(self, error: BaseException | None) -> None:
        self._recorder.completed(
            status_code=200,
            text=self._tee.text,
            usage=self._tee.usage,
            truncated=self._tee.truncated,
        )
        if error is not None:
            # The status line said 200 long before this happened, and it did: the client
            # received a partial response. Recording the status honestly and the cause
            # separately is what keeps the error taxonomy from claiming a 200 failed.
            self._recorder.stream_ended_early(error)
        self._recorder.submit()


class RequestLogService:
    """What the data plane holds: a policy source, a sink, and a way to start a record."""

    def __init__(self, queue: LogQueue, flusher: LogFlusher) -> None:
        self._queue = queue
        self._flusher = flusher

    @property
    def sink(self) -> LogSink:
        return self._queue

    def start(self) -> None:
        self._flusher.start()

    async def stop(self) -> None:
        await self._flusher.stop()

    async def flush_pending(self) -> int:
        return await self._flusher.flush_pending()

    def begin(
        self,
        *,
        organization_id: uuid.UUID,
        gateway_id: uuid.UUID,
        policy: LogPolicy,
        api_key_id: uuid.UUID | None = None,
        request_id: str | None = None,
    ) -> RequestRecorder:
        return RequestRecorder(
            RequestRecord(
                organization_id=organization_id,
                gateway_id=gateway_id,
                api_key_id=api_key_id,
                request_id=request_id,
                policy=policy,
            ),
            self._queue,
        )


def as_json(value: Any) -> Any:
    """JSON-safe form of a body, for the insert. Anything unserialisable becomes text.

    Bodies come from client requests, which can contain anything the OpenAI schema allows
    plus whatever a provider has added since. A body that cannot be serialised must not
    fail the batch it happens to share with ninety-nine well-formed ones.
    """
    if value is None:
        return None
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return [{"role": "system", "content": "[unserialisable body]"}]
    return value


__all__ = [
    "BATCH_SIZE",
    "FLUSH_INTERVAL_SECONDS",
    "MAX_RESPONSE_CHARS",
    "QUEUE_MAX_RECORDS",
    "LogFlusher",
    "LogPolicy",
    "LogQueue",
    "LogSink",
    "LogWriter",
    "NullSink",
    "RequestLogService",
    "RequestRecord",
    "RequestRecorder",
    "StreamRecorder",
    "StreamTee",
    "as_json",
]
