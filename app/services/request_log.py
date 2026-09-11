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
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from app.adapters.base import UpstreamTarget, get_adapter
from app.core.errors import AppError
from app.core.ids import uuid7
from app.core.metrics import LogMetrics, ProxyMetrics
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
    #: Whether this gateway's traffic may feed conversation memory (SPEC §10.2, task 13).
    #: Resolved with the rest of the policy so that a gateway switched off mid-stream still
    #: distils the request that was already in flight the way it was configured when it
    #: started — the same snapshot rule as everything else here.
    distillation: bool = False

    @classmethod
    def of(cls, config: LoggingConfig) -> LogPolicy:
        return cls(
            request_body=config.log_request_body,
            assembled_prompt=config.log_assembled_prompt,
            response_body=config.log_response_body,
            redaction_patterns=tuple(config.redaction_patterns),
            # Both halves, because the schema's validator only refuses the combination on
            # *write*: a row stored before that rule existed can still say "distil without
            # bodies", and the answer to that is to distil nothing rather than to read a
            # transcript that was never captured.
            distillation=config.enable_distillation and config.log_request_body,
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
    #: Task 100. Which of the injected chunks the answer actually cited, by id, in the
    #: order it first cited them — ``retrieved_chunk_ids`` is what went *in*, this is
    #: what was *used*, and the ratio between them is the relevance signal task 103
    #: reads. Filled whatever the gateway's citation mode.
    cited_chunk_ids: list[str] = field(default_factory=list)
    #: Handles in the answer that pointed at no injected chunk — ``[7]`` with six
    #: injected. A count rather than the list, because the number is the signal: a model
    #: citing chunks it was never shown is making things up in the one place it was
    #: asked not to.
    citations_unresolved: int = 0
    #: Task 101. What the prompt was measured with and what it measured, beside the
    #: provider's own ``prompt_tokens``: the calibration is the ratio of the two.
    tokenizer: str | None = None
    estimated_prompt_tokens: int | None = None
    failover_attempts: list[Any] = field(default_factory=list)
    #: Generation parameters the target's dialect could not carry, so they never reached
    #: the provider (SPEC §8.3). Empty for an OpenAI-shaped upstream, which is every
    #: request that does not go to Claude — see :meth:`RequestRecorder.prepared`.
    dropped_params: list[str] = field(default_factory=list)

    request_id: str | None = None
    response_truncated: bool = False
    #: SPEC §8.2. The upstream died after the response had already begun, so failover was
    #: no longer possible and the client received a partial answer under a 200.
    failed_after_stream_start: bool = False
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

    def __init__(
        self,
        record: RequestRecord,
        sink: LogSink,
        *,
        metrics: ProxyMetrics | None = None,
        gateway: str = "",
    ) -> None:
        self._record = record
        self._sink = sink
        #: Prometheus, alongside the row. The row is per request and queryable for a day
        #: or two; these are aggregates that survive retention and are what an alert reads.
        #: ``gateway`` is the slug rather than the id because the label is read by a human
        #: on a dashboard, and the id is on the row for anyone who needs to join.
        self._metrics = metrics
        self._gateway = gateway or "unknown"
        self._started = time.perf_counter()
        self._upstream_started: float | None = None
        self._submitted = False
        #: The generation parameters the caller explicitly asked for, kept from
        #: :meth:`client_request` until :meth:`prepared` knows which dialect has to carry
        #: them. Names only — the values are in the transcript, and this is a question
        #: about which knobs exist rather than what they were set to.
        self._asked_for: set[str] = set()

    @property
    def record(self) -> RequestRecord:
        return self._record

    @property
    def policy(self) -> LogPolicy:
        return self._record.policy

    def client_request(self, request: ChatRequest) -> None:
        """The caller's own messages, before any layer was prepended."""
        self._record.streamed = bool(request.stream)
        self._asked_for = set(request.client_parameters())
        if self._record.policy.request_body:
            self._record.request_body = _messages(request.messages)

    def prepared(
        self,
        messages: Sequence[ChatMessage],
        target: UpstreamTarget,
        *,
        tokenizer: str | None = None,
        estimated_tokens: int | None = None,
    ) -> None:
        """What is about to go upstream, after assembly and the parameter merge.

        Called once per routing attempt, not once per request: two targets can carry
        different system contexts, so the stored prompt has to be the one the target that
        answered actually received. The last call wins, and the last call is the attempt
        that served — or, when the whole chain failed, the last one tried.

        The model's *name* is copied as well as its id, because the row has to survive
        that model being deleted — see the note on foreign keys in
        :mod:`app.db.models.request_log`.
        """
        if self._record.policy.assembled_prompt:
            self._record.assembled_prompt = _messages(messages)
        self._record.upstream_model_id = target.id
        self._record.model_name = target.name
        self._record.dropped_params = _dropped(target, self._asked_for)
        self._record.tokenizer = tokenizer
        self._record.estimated_prompt_tokens = estimated_tokens

    def end_user(self, *, end_user_id: uuid.UUID | None, session_id: str | None) -> None:
        """Who this request belongs to, and which conversation (SPEC §6.2).

        Both nullable and both meaning "not known", which is different from "nobody":
        a caller who sent no identity, or a gateway that declines to invent one, leaves
        these null and the row is still complete. Task 13 reads exactly this pair to
        decide what to distil, so a request with no end user is one it correctly skips.
        """
        self._record.end_user_id = end_user_id
        self._record.session_id = session_id

    def retrieval(self, *, latency_ms: int | None) -> None:
        """How long the memory subsystem took, when it ran at all.

        ``None`` for a gateway with no connectors attached, and that is not the same as
        zero: a zero would claim the gateway measured retrieval and found it instant,
        which would drag the p95 chart toward a number nobody waited for. The column is
        nullable for exactly this reason.
        """
        self._record.latency_retrieval_ms = latency_ms

    def injected(
        self,
        *,
        tokens: int,
        chunks: Sequence[Mapping[str, Any]],
        facts: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """What memory put into the prompt, per routing attempt.

        Plain mappings rather than a retrieval type, for the same reason
        :meth:`attempts` takes dictionaries: this module records what happened and has no
        business importing the subsystem that made it happen. The shape is a jsonb
        column's shape anyway.

        Called once per attempt, and the last call wins — matching :meth:`prepared`,
        because two targets with different context windows can inject different amounts
        and the row must describe the one that answered.
        """
        self._record.memory_tokens = tokens
        self._record.retrieved_chunk_ids = [dict(chunk) for chunk in chunks]
        self._record.retrieved_fact_ids = [dict(fact) for fact in facts]

    def cited(self, chunk_ids: Sequence[str], *, unresolved: int = 0) -> None:
        """Which injected chunks the answer cited (task 100).

        Plain ids rather than the resolver's types, for the same reason :meth:`injected`
        takes mappings: the log records what happened without importing the subsystem
        that worked it out. The row already carries each chunk's name and section under
        ``retrieved_chunk_ids``, so an id here is enough for the drawer to mark it.
        """
        self._record.cited_chunk_ids = [str(chunk_id) for chunk_id in chunk_ids]
        self._record.citations_unresolved = max(0, int(unresolved))

    def attempts(self, records: Sequence[Mapping[str, Any]]) -> None:
        """The routing chain, already in its JSON form.

        Plain dictionaries rather than a routing type, so :mod:`app.services.routing` can
        import the proxy without this module and the resolver importing routing back. The
        shape is a jsonb column's shape anyway.
        """
        self._record.failover_attempts = [dict(record) for record in records]

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

    def throttled(self) -> None:
        """A limit refused this request: keep the row, drop the transcript.

        SPEC §11 wants throttling visible per gateway, so the metadata row is the point —
        it is what puts a rate-limit series on the error chart and the caller in the "top
        throttled end users" list. The bodies are not: nothing was done with them, no
        model saw them, and storing end-user text for a request that never happened is
        cost and exposure with no reader.

        Recorded as a *reason* rather than by simply not collecting, because the drawer
        has to be able to say why a row it is showing has no transcript.
        """
        self._record.request_body = None
        self._record.assembled_prompt = None
        self._record.response_body = None
        self._record.bodies_omitted = "rate_limited"

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
        """A stream that started successfully and did not finish.

        ``failed_after_stream_start`` is set for an upstream failure and *not* for a
        client hang-up, which is the distinction SPEC §8.2's flag is for: it means "this
        could not be failed over because the response had already begun", and a caller
        walking away is not something failover would have rescued. Both still leave a row
        with a 200, because the status line went out long before either happened.
        """
        cancelled = isinstance(error, asyncio.CancelledError)
        self._record.error_code = "client_disconnected" if cancelled else "stream_failed"
        self._record.error_message = type(error).__name__
        self._record.failed_after_stream_start = not cancelled

    def submit(self) -> None:
        """Hand the record over. Safe to call twice; the second call does nothing."""
        if self._submitted:
            return
        self._submitted = True
        self._record.latency_total_ms = self._elapsed_ms()
        self._observe()
        try:
            self._sink.submit(self._record)
        except Exception:  # pragma: no cover - a sink that raises is a bug, not a 500
            logger.warning("could not submit a request log record", exc_info=True)

    def _observe(self) -> None:
        """Count the request, and record what the gateway itself cost.

        The overhead is total minus upstream, which is SPEC §4.2's budget measured on real
        traffic. It is only recorded when an upstream call actually happened: a request
        refused by the rate limiter never had one, and folding those in as pure overhead
        would make throttling look like a latency regression.

        Guarded like everything else on this class — a labelling mistake must not turn a
        served completion into a 500.
        """
        if self._metrics is None:
            return
        record = self._record
        try:
            self._metrics.requests.labels(
                gateway=self._gateway,
                model=record.model_name or "none",
                status=str(record.status_code),
            ).inc()
            self._metrics.duration.labels(gateway=self._gateway).observe(
                record.latency_total_ms / 1000
            )
            if record.latency_upstream_ms is not None:
                overhead = max(0, record.latency_total_ms - record.latency_upstream_ms)
                self._metrics.overhead.labels(gateway=self._gateway).observe(overhead / 1000)
            self._observe_citations(record)
            self._observe_drift(record)
        except Exception:  # pragma: no cover - a metrics failure is not a request failure
            logger.warning("could not record proxy metrics", exc_info=True)

    def _observe_drift(self, record: RequestRecord) -> None:
        """Task 101's gauge: the provider's count over ours, per model, over a rolling
        window of recent requests. Only when both numbers exist — the same rule the
        metrics store's calibration query applies, so the gauge and the screen agree."""
        if self._metrics is None or self._metrics.drift is None:
            return
        if (
            record.status_code >= 400
            or record.estimated_prompt_tokens is None
            or record.prompt_tokens is None
            or record.model_name is None
        ):
            return
        ratio = self._metrics.drift.add(
            record.model_name,
            estimated=record.estimated_prompt_tokens,
            reported=record.prompt_tokens,
        )
        if ratio is not None:
            self._metrics.tokenizer_drift.labels(model=record.model_name).set(ratio)

    def _observe_citations(self, record: RequestRecord) -> None:
        """Task 100's three counters.

        ``uncited`` is the one worth alerting on and the one that needs care: it counts
        requests that *injected* documents and cited none, so a gateway with no memory
        attached does not show up as a gateway whose memory is ignored. Only successful
        answers — a 5xx cited nothing because there was no answer, not because the
        documents were irrelevant.
        """
        assert self._metrics is not None
        if record.cited_chunk_ids:
            self._metrics.citations_resolved.labels(gateway=self._gateway).inc(
                len(record.cited_chunk_ids)
            )
        if record.citations_unresolved:
            self._metrics.citations_unresolved.labels(gateway=self._gateway).inc(
                record.citations_unresolved
            )
        injected = any(
            isinstance(entry, Mapping) and entry.get("injected") is True
            for entry in record.retrieved_chunk_ids
        )
        if injected and not record.cited_chunk_ids and record.status_code < 400:
            self._metrics.uncited_requests.labels(gateway=self._gateway).inc()

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


def _dropped(target: UpstreamTarget, asked_for: Collection[str]) -> list[str]:
    """Which of this request's generation parameters the target's dialect cannot carry.

    Answered here rather than by the adapter mid-call, because the point is the *record*:
    a silently dropped ``presence_penalty`` is otherwise discoverable only by experiment,
    and six months later it is a support ticket nobody can close (SPEC §8.3).

    Two of the three parameter layers are visible from here — what the caller sent, and
    the model's own ``default_params``. The gateway's ``param_overrides`` are not, because
    a gateway can point at several models in different dialects at once and the merge that
    combines them belongs to the request, not to this record. That layer is operator
    configuration rather than caller intent, which is the half this is for.
    """
    try:
        adapter = get_adapter(target.dialect)
    except ValueError:  # pragma: no cover - the proxy refuses this request first
        return []
    return list(adapter.dropped({*asked_for, *target.default_params}))


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


class TranscriptSubscriber(Protocol):
    """Somebody who wants to know that transcripts have landed (task 13).

    One method, called after the batch is committed and never before. A protocol rather
    than a direct reference to :class:`~app.services.distillation_trigger.
    DistillationTrigger`, because this module is the request path's logging and must not
    grow an import of the memory subsystem — and because a worker process builds a flusher
    with no subscriber at all.
    """

    async def consider(self, records: Sequence[RequestRecord]) -> Any: ...


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
        subscriber: TranscriptSubscriber | None = None,
    ) -> None:
        self._queue = queue
        self._writer = writer
        self._metrics = metrics
        self._batch_size = batch_size
        self._interval = interval_seconds
        self._redaction_budget = redaction_budget_seconds
        self._subscriber = subscriber
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
        await self._notify(prepared)

    async def _notify(self, prepared: Sequence[RequestRecord]) -> None:
        """Tell the subscriber the transcripts are committed. Never fails upward.

        After the write, so a job cannot arrive at a worker before the row it is about
        exists. Failing here costs the conversation memory those requests would have
        produced; failing *upward* would lose the log batch that has already been written,
        which would be a strictly worse trade for a strictly less important feature.
        """
        if self._subscriber is None:
            return
        try:
            await self._subscriber.consider(prepared)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("could not queue distillation for a batch", exc_info=True)

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

    def __init__(
        self, queue: LogQueue, flusher: LogFlusher, *, metrics: ProxyMetrics | None = None
    ) -> None:
        self._queue = queue
        self._flusher = flusher
        self._metrics = metrics

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
        gateway_slug: str = "",
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
            metrics=self._metrics,
            # Not stored on the record — the row already carries the id, and a slug can be
            # renamed while a row cannot. It exists here only as a metric label.
            gateway=gateway_slug,
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
    "TranscriptSubscriber",
    "as_json",
]
