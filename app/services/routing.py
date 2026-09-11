"""Which upstream serves a request, and what happens when it does not.

SPEC §8.1 gives a gateway one routing mode over an ordered list of targets. Three modes,
and the difference between them is entirely about failure:

* ``single`` — one target. A failure is the client's problem.
* ``failover`` — a priority chain. A *retryable* failure moves to the next target; the
  client sees an error only when the chain is exhausted.
* ``ab_split`` — one target chosen by weight. **No retry**, on purpose: a retried A/B
  request would land on the other variant and quietly corrupt the comparison the mode
  exists to enable. That is a data-integrity rule rather than a performance trade, so it
  is enforced by the plan having length 1 and not by a flag somebody can flip.

Four decisions here are worth stating.

**Retry classification is a table, not a chain of conditionals.** SPEC §8.2 names eleven
status codes and splits them down the middle: 408/429/500/502/503/504 are worth another
target, 400/401/403/404/422 are not, because the next target would reject them
identically and trying it turns one bad request into two. :data:`RETRY_TABLE` is that
list as data, so a test can iterate it and a future change is one line rather than a
re-reading of a function. Transport failures never arrive here as transport exceptions:
the proxy has already turned a DNS failure or a refused connection into a 502 and a read
timeout into a 504, so classification is one lookup and connect errors cannot be
forgotten.

**The deadline is enforced, not merely checked.** Every attempt already carries the
target model's own ``timeout_seconds``, applied per read by the adapter — but three
targets at 60 s each is three minutes, and no client waits that long. Each attempt runs
inside :func:`asyncio.timeout` on the *remaining* budget, so a chain cannot outlive it
even when a single target is slower than the whole allowance. What the deadline does not
cover is the frame relay of a stream that has already started: the response is on the
wire by then, and cutting it off would turn a slow generation into a truncated one.

**Backoff is jittered, and it is not about this request.** Fifty to a hundred and fifty
milliseconds does nothing for one caller. It exists so that a fleet of workers that all
meet a provider's 503 in the same instant do not all retry in the same instant — the
synchronised second wave is what turns a provider blip into a provider outage.

**Selection is sticky when it can be.** ``crc32(f"{key}:{gateway_id}")`` against
cumulative weight bands, so one end user sees one variant for as long as the weights
hold. The gateway id is in the hash because without it somebody unlucky enough to land in
the bottom band would land there on every gateway in the account, and the A/B test would
be measuring that person rather than the model. Changing the weights re-buckets everyone;
that is documented and accepted, because the alternative is an assignment table that has
to be written, read and expired on the request path.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
import zlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.adapters.base import UpstreamTarget
from app.api.proxy.errors import ProxyError, UpstreamTimeout
from app.core.metrics import RoutingMetrics
from app.schemas.openai import ChatRequest, ChatResponse
from app.services.citations import Resolution
from app.services.gateway_resolver import ResolvedGateway
from app.services.proxy import Prepared, ProxyService, StreamObserver, UpstreamStream
from app.services.retrieval import Recall

logger = logging.getLogger(__name__)

#: SPEC §8.2, as data. Present-and-``True`` means try the next target; present-and-
#: ``False`` means the next target would say the same thing. A code in neither half is
#: treated as final: an unrecognised failure is not evidence that retrying will help, and
#: guessing costs the client another provider round trip.
RETRY_TABLE: Mapping[int, bool] = {
    # Retryable: the request is fine, this target is not.
    408: True,  # request timeout
    429: True,  # rate limited — another provider has another bucket
    500: True,
    502: True,  # also what the proxy raises for a connect or DNS failure
    503: True,  # also what the gateway raises for an undecryptable credential
    504: True,  # also what the proxy raises for a read timeout
    # Final: the request itself is the problem.
    400: False,
    401: False,  # per-target in principle; see the note in `is_retryable`
    403: False,
    404: False,
    422: False,
}

#: Overall budget for a whole routing chain. Deliberately larger than the 60 s default
#: model timeout, so a single-target gateway behaves exactly as it did before this
#: existed, and small enough that a three-deep chain of hung providers still answers.
DEFAULT_DEADLINE_SECONDS = 120.0

#: Jittered pause between attempts. See the module docstring — this is fleet behaviour,
#: not client behaviour.
BACKOFF_MIN_SECONDS = 0.05
BACKOFF_MAX_SECONDS = 0.15


def jitter() -> float:
    return random.uniform(BACKOFF_MIN_SECONDS, BACKOFF_MAX_SECONDS)


def is_retryable(error: BaseException) -> bool:
    """Whether another target is worth trying.

    Keyed on the status the client would otherwise have seen. A 401 is not retried even
    though the next target has its own credential: a genuinely per-target authentication
    failure shows up as a 401 on every target in turn, which is a configuration problem to
    fix rather than latency to spend on every request forever.
    """
    if not isinstance(error, ProxyError):
        # A bug in this process, not a failure of an upstream. Running it again with the
        # same inputs produces the same result.
        return False
    return RETRY_TABLE.get(error.status_code, False)


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoutingPlan:
    """The targets to try, in order."""

    mode: str
    targets: tuple[UpstreamTarget, ...]

    @property
    def can_retry(self) -> bool:
        return self.mode == "failover"


def plan(gateway: ResolvedGateway, *, end_user_key: str | None = None) -> RoutingPlan:
    """Resolve a gateway to an ordered attempt list.

    Raises :class:`GatewayUnavailable` when there is nothing to try, with the message that
    names the fix — a disabled model and an unconfigured gateway need different actions.
    """
    targets = gateway.require_targets()

    if gateway.routing_mode == "ab_split":
        chosen = select(targets, gateway.weights, gateway_id=gateway.id, key=end_user_key)
        return RoutingPlan(mode="ab_split", targets=(chosen,))

    if gateway.routing_mode == "failover":
        return RoutingPlan(mode="failover", targets=targets)

    # `single`, and anything a future migration adds that this build does not know: one
    # target, no retry. An unknown mode must not silently become a fan-out.
    return RoutingPlan(mode="single", targets=targets[:1])


def select(
    targets: Sequence[UpstreamTarget],
    weights: Mapping[uuid.UUID, int],
    *,
    gateway_id: uuid.UUID,
    key: str | None,
) -> UpstreamTarget:
    """One target, by weighted selection over cumulative bands.

    Weights are validated at save time to sum to 100, so ``% total`` and SPEC §8.1's
    ``% 100`` are the same arithmetic. Dividing by the real total anyway is what keeps a
    chain written outside the API — a migration, a fixture, or a disabled model dropping
    out of the list — from silently sending a slice of traffic nowhere.
    """
    bands = [max(0, weights.get(target.id, 0)) for target in targets]
    total = sum(bands)
    if total <= 0:
        # Every weight is zero, or the weighted ones were disabled out of the chain.
        # Serving uniformly beats refusing to serve, but it is a silent change to what an
        # experiment is measuring, so it is said out loud rather than merely survived.
        logger.warning(
            "gateway has no usable A/B weights; selecting a target uniformly",
            extra={"gateway_id": str(gateway_id)},
        )
        return random.choice(list(targets))

    if key is None:
        return _band(targets, bands, random.randrange(total))

    return _band(targets, bands, zlib.crc32(f"{key}:{gateway_id}".encode()) % total)


def _band(targets: Sequence[UpstreamTarget], bands: Sequence[int], bucket: int) -> UpstreamTarget:
    running = 0
    for target, weight in zip(targets, bands, strict=True):
        running += weight
        if bucket < running:
            return target
    return targets[-1]  # pragma: no cover - bucket < total makes this unreachable


# ---------------------------------------------------------------------------
# what happened
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Attempt:
    """One upstream call, successful or not. SPEC §10.2's ``failover_attempts`` entry."""

    target_id: uuid.UUID
    model_name: str
    status: int
    error_code: str | None
    latency_ms: int
    #: Whether this failure *class* justifies another target — not whether one was
    #: actually tried. The last link in a chain still records ``true`` for a 503, which is
    #: what tells an operator the chain was too short rather than the error final.
    retryable: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "target_id": str(self.target_id),
            "model_name": self.model_name,
            "status": self.status,
            "error_code": self.error_code,
            "latency_ms": self.latency_ms,
            "retryable": self.retryable,
        }


@dataclass
class Attempts:
    """The history of one chain, owned by the caller.

    An out-parameter rather than a return value, because the caller needs it on the path
    where the chain *raises*: a gateway whose every target failed is precisely the request
    somebody opens the detail drawer for, and a history that only survived success would
    be missing exactly then.

    ``on_prepared`` is how the request log learns what was actually sent without this
    module importing it. Each attempt re-runs prompt assembly — two targets can carry
    different system contexts and different default parameters — so the stored transcript
    has to follow the attempts rather than be captured once up front.
    """

    on_prepared: Callable[[Prepared], None] | None = None
    records: list[Attempt] = field(default_factory=list)

    def prepared(self, prepared: Prepared) -> None:
        if self.on_prepared is not None:
            self.on_prepared(prepared)

    def record(
        self,
        target: UpstreamTarget,
        *,
        status: int,
        error_code: str | None,
        latency_ms: int,
        retryable: bool,
    ) -> None:
        self.records.append(
            Attempt(
                target_id=target.id,
                model_name=target.name,
                status=status,
                error_code=error_code,
                latency_ms=latency_ms,
                retryable=retryable,
            )
        )

    def as_json(self) -> list[dict[str, Any]]:
        """The array for the log row — empty unless more than one target was involved.

        A chain that succeeded on its first attempt is described completely by the row's
        own ``upstream_model_id``, ``status_code`` and ``latency_upstream_ms`` columns,
        and that is the overwhelming majority of traffic. A one-element array on every
        request would spend storage on every row restating what three columns already say.
        Non-empty therefore means "more than one target was involved", which is exactly
        when the drawer draws a timeline.
        """
        if len(self.records) < 2:
            return []
        return [record.as_json() for record in self.records]

    def __len__(self) -> int:
        return len(self.records)


@dataclass(frozen=True, slots=True)
class Completed:
    prepared: Prepared
    response: ChatResponse


@dataclass(frozen=True, slots=True)
class Opened:
    prepared: Prepared
    stream: UpstreamStream


# ---------------------------------------------------------------------------
# the executor
# ---------------------------------------------------------------------------


class Router:
    """Walks a plan, one attempt at a time, until something answers or nothing can.

    Wraps :class:`~app.services.proxy.ProxyService` rather than living inside it. The
    proxy's job is one call to one provider; keeping "which provider, and what if it
    fails" out of it is what lets either be read on its own.
    """

    def __init__(
        self,
        proxy: ProxyService,
        *,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        backoff: Callable[[], float] = jitter,
        metrics: RoutingMetrics | None = None,
    ) -> None:
        self._proxy = proxy
        self._deadline = deadline_seconds
        self._backoff = backoff
        self._metrics = metrics

    async def complete(
        self,
        request: ChatRequest,
        gateway: ResolvedGateway,
        routing: RoutingPlan,
        attempts: Attempts,
        *,
        recall: Recall | None = None,
    ) -> Completed:
        prepared, response = await self._run(
            request, gateway, routing, attempts, self._proxy.complete, recall=recall
        )
        return Completed(prepared=prepared, response=response)

    async def open_stream(
        self,
        request: ChatRequest,
        gateway: ResolvedGateway,
        routing: RoutingPlan,
        attempts: Attempts,
        *,
        observer: StreamObserver | None = None,
        recall: Recall | None = None,
        on_citations: Callable[[Resolution], None] | None = None,
    ) -> Opened:
        """Start a stream, failing over **only while the status line is still ours**.

        :meth:`ProxyService.open_stream` sends the request and validates the response
        status before yielding anything, so a provider 503 on a streamed request is still
        an ordinary HTTP failure and the next target can be tried. The moment this
        returns, the response has begun and the window is closed — SPEC §8.2. Nothing is
        buffered to widen it: holding frames back until a generation looks healthy would
        trade the whole point of streaming for a rare recovery.
        """

        async def call(prepared: Prepared) -> UpstreamStream:
            return await self._proxy.open_stream(
                prepared, observer=observer, on_citations=on_citations
            )

        prepared, stream = await self._run(request, gateway, routing, attempts, call, recall=recall)
        return Opened(prepared=prepared, stream=stream)

    async def _run[T](
        self,
        request: ChatRequest,
        gateway: ResolvedGateway,
        routing: RoutingPlan,
        attempts: Attempts,
        call: Callable[[Prepared], Awaitable[T]],
        *,
        recall: Recall | None = None,
    ) -> tuple[Prepared, T]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._deadline
        last: ProxyError | None = None

        for index, target in enumerate(routing.targets):
            if index:
                # Bounded by what is left, so the pause itself cannot push the chain past
                # its own deadline.
                await asyncio.sleep(min(self._backoff(), max(0.0, deadline - loop.time())))

            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    "routing deadline reached before the next target could be tried",
                    extra={"gateway_slug": gateway.slug, "attempted": index},
                )
                break

            # Re-assembled per attempt, with the *same* retrieval: the two targets can
            # carry different system contexts and different context windows, so the
            # prompt differs even though what was retrieved does not.
            prepared = self._proxy.prepare(request, gateway, target, recall=recall)
            attempts.prepared(prepared)
            started = time.perf_counter()

            try:
                async with asyncio.timeout(remaining):
                    result = await call(prepared)
            except TimeoutError:
                # The chain's deadline, not the model's own read timeout — that one
                # arrives as an `UpstreamTimeout` through the branch below.
                error: ProxyError = UpstreamTimeout(
                    f"[upstream:{target.name}] the gateway's overall "
                    f"{self._deadline:g}s deadline was reached."
                )
            except ProxyError as exc:
                error = exc
            else:
                attempts.record(
                    target,
                    status=200,
                    error_code=None,
                    latency_ms=_ms(started),
                    retryable=False,
                )
                self._count(routing, target, outcome="served")
                self._chain_length(len(attempts))
                return prepared, result

            retryable = is_retryable(error)
            attempts.record(
                target,
                status=error.status_code,
                error_code=error.code,
                latency_ms=_ms(started),
                retryable=retryable,
            )
            self._count(routing, target, outcome="failed")
            last = error

            if not (routing.can_retry and retryable):
                break

            self._failover(target, error)
            logger.info(
                "upstream failed; trying the next target",
                extra={
                    "gateway_slug": gateway.slug,
                    "model": target.name,
                    "error_code": error.code,
                    "status_code": error.status_code,
                },
            )

        self._chain_length(len(attempts))
        # `last` is unset only when the deadline had already passed before the first
        # attempt, which means the plan was empty of usable time rather than of targets.
        raise last or UpstreamTimeout(
            f"[gateway:{gateway.slug}] the overall {self._deadline:g}s deadline was reached "
            f"before any upstream could be tried."
        )

    # -- metrics ---------------------------------------------------------

    def _count(self, routing: RoutingPlan, target: UpstreamTarget, *, outcome: str) -> None:
        if self._metrics is not None:
            self._metrics.attempts.labels(
                mode=routing.mode, model=target.name, outcome=outcome
            ).inc()

    def _failover(self, target: UpstreamTarget, error: ProxyError) -> None:
        if self._metrics is not None:
            # Counted per *source* target: "which of my upstreams keeps making me fail
            # over" is the question this answers, and the answer names a model to fix.
            self._metrics.failovers.labels(model=target.name, error_code=error.code).inc()

    def _chain_length(self, attempted: int) -> None:
        if self._metrics is not None and attempted:
            self._metrics.chain_length.observe(attempted)


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


__all__ = [
    "BACKOFF_MAX_SECONDS",
    "BACKOFF_MIN_SECONDS",
    "DEFAULT_DEADLINE_SECONDS",
    "RETRY_TABLE",
    "Attempt",
    "Attempts",
    "Completed",
    "Opened",
    "Router",
    "RoutingPlan",
    "is_retryable",
    "jitter",
    "plan",
    "select",
]
