"""Enforcing SPEC §11 on one request, and getting out of the way when it cannot.

:mod:`app.services.limits` decides what the rules are and :mod:`app.services.limit_store`
holds the counters. This is the object the request path talks to: one per request, created
before anything expensive happens and asked two questions at two moments.

**Two phases, because the two halves cost different amounts.** Request counters need
nothing but the gateway and who is asking, so they are checked before routing, before
retrieval, before a single token has been counted — a throttled client pays for an
authentication and one Redis round trip. Token limits cannot be checked that early,
because SPEC §11 counts *injected memory* too and memory does not exist until retrieval has
run; they are checked immediately before dispatch, together with the concurrency slot,
which is deliberately held for the upstream call and nothing else.

The consequence is worth stating plainly: a request refused on tokens has already spent a
request against the minute. That is not a leak, it is a request — but it does mean the two
phases are atomic individually and not jointly, and the alternative (refunding phase one)
reintroduces exactly the read-modify-write the Lua script exists to avoid.

**Fail-open is a policy, not an accident.** Every call into the store is wrapped, and what
happens next is ``rate_limit_fail_open``. Open serves the request and counts it; closed
refuses with a 503. Neither is right for everybody: during a Redis outage the open one
leaves the shared upstream key unprotected and the closed one turns a cache outage into an
outage. What matters is that it is one setting, in one place, with a metric behind it.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING

from app.api.proxy.errors import RateLimitUnavailable
from app.core.errors import RateLimited
from app.services.limit_store import Consumed, Consumption, Hold, LimitStore
from app.services.limits import (
    EARLY_LIMITS,
    LATE_LIMITS,
    NO_CEILINGS,
    Ceilings,
    Effective,
    Reading,
    Rule,
    effective,
    headers,
    message,
    plan,
    retry_after,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle only exists for the type checker
    from app.core.metrics import RateLimitMetrics
    from app.services.gateway_resolver import ResolvedGateway

logger = logging.getLogger(__name__)


class RateLimiter:
    """The limits of every gateway, over one shared store."""

    def __init__(
        self,
        store: LimitStore,
        *,
        ceilings: Ceilings = NO_CEILINGS,
        fail_open: bool = True,
        metrics: RateLimitMetrics | None = None,
    ) -> None:
        self._store = store
        self._ceilings = ceilings
        self._fail_open = fail_open
        self._metrics = metrics

    @property
    def ceilings(self) -> Ceilings:
        return self._ceilings

    def resolve(self, gateway: ResolvedGateway) -> Effective:
        """What this gateway's limits actually come to, ceiling included.

        Public because the Limits screen asks the same question the request path does,
        and two implementations of "what is the effective limit" would eventually
        disagree on the screen that exists to explain the enforcement.
        """
        return effective(
            gateway.limits, ceilings=self._ceilings, global_models=gateway.global_models
        )

    def begin(
        self, gateway: ResolvedGateway, *, end_user_id: uuid.UUID | None, holder: str
    ) -> RequestLimits:
        return RequestLimits(
            self,
            limits=self.resolve(gateway),
            gateway_id=gateway.id,
            end_user_id=end_user_id,
            holder=holder,
        )

    # -- the store, with the policy wrapped around it ------------------------

    async def consume(self, consumptions: list[Consumption], *, holder: str) -> Consumed | None:
        """``None`` means the store could not answer and the caller should fail open."""
        if not consumptions:
            return Consumed()
        started = time.perf_counter()
        try:
            return await self._store.consume(consumptions, holder=holder)
        except Exception:
            # Broad on purpose: redis-py raises its own errors *and* bare OSError on a
            # refused connection, and the response to every one of them is this policy.
            self._unavailable()
            return None
        finally:
            self._observe(time.perf_counter() - started)

    async def give_back(self, holds: tuple[Hold, ...]) -> None:
        if not holds:
            return
        try:
            await self._store.release(holds)
        except Exception:
            # The lease reclaims the slot. A failed release costs at most one slot for
            # the lease's duration, which is the failure this design was chosen for.
            logger.warning("could not release a concurrency slot", exc_info=True)

    async def settle_tokens(self, adjustments: list[tuple[Rule, int]]) -> None:
        if not adjustments:
            return
        try:
            await self._store.settle(adjustments)
        except Exception:
            logger.warning("could not settle token usage", exc_info=True)

    def _unavailable(self) -> None:
        logger.warning(
            "rate limiter unavailable",
            extra={"policy": "fail_open" if self._fail_open else "fail_closed"},
            exc_info=True,
        )
        if self._metrics is not None:
            self._metrics.unavailable.labels(
                policy="fail_open" if self._fail_open else "fail_closed"
            ).inc()

    def _observe(self, seconds: float) -> None:
        if self._metrics is not None:
            self._metrics.duration.observe(seconds)

    def observe(self, reading: Reading, *, rejected: bool) -> None:
        if self._metrics is None:
            return
        labels = {"limit": reading.rule.limit, "scope": reading.rule.scope}
        if rejected:
            self._metrics.rejections.labels(**labels).inc()
        elif reading.near_limit:
            self._metrics.near_limit.labels(**labels).inc()

    @property
    def fail_open(self) -> bool:
        return self._fail_open


class RequestLimits:
    """One request's passage through its gateway's limits.

    Created even when the gateway has no limits at all, because the alternative is a
    ``None`` threaded through the request path and four places that have to remember to
    check it. With nothing configured every method here returns immediately and no
    network call is made.
    """

    def __init__(
        self,
        limiter: RateLimiter,
        *,
        limits: Effective,
        gateway_id: uuid.UUID,
        end_user_id: uuid.UUID | None,
        holder: str,
    ) -> None:
        self._limiter = limiter
        self._limits = limits
        self._gateway_id = gateway_id
        self._end_user_id = end_user_id
        self._holder = holder
        self._readings: list[Reading] = []
        self._holds: tuple[Hold, ...] = ()
        self._token_rules: tuple[Rule, ...] = ()
        self._estimate = 0
        #: True once the store has failed and this request was served anyway. The route
        #: does not use it; the log and the metric do.
        self.degraded = False

    @property
    def enforced(self) -> bool:
        return not self._limits.unlimited

    @property
    def needs_estimate(self) -> bool:
        """Whether a token limit applies, and therefore whether counting is worth it.

        The request path assembles the prompt a second time to answer :meth:`tokens`, and
        that work is skipped entirely when no ``tokens_per_minute`` is configured at
        either scope — which is the default, so most gateways never pay for it.
        """
        return self._limits.gateway.tokens_per_minute is not None or (
            self._limits.per_end_user.tokens_per_minute is not None
            and self._end_user_id is not None
        )

    @property
    def readings(self) -> tuple[Reading, ...]:
        return tuple(self._readings)

    # -- the two phases ------------------------------------------------------

    async def requests(self) -> None:
        """SPEC §11's request counters, before anything has been spent on this request."""
        rules = self._plan(EARLY_LIMITS)
        await self._apply([Consumption(rule=rule, cost=1) for rule in rules])

    async def tokens(self, estimate: int) -> None:
        """The token cap and the concurrency slot, immediately before dispatch.

        ``estimate`` is the assembled prompt — retrieved chunks, injected facts and the
        gateway's system context included — because SPEC §11's cap is on what the provider
        is asked to read, and memory is the part of that the client did not send and
        cannot see.
        """
        rules = self._plan(LATE_LIMITS)
        self._token_rules = tuple(rule for rule in rules if rule.limit == "tokens_per_minute")
        self._estimate = max(0, estimate)
        await self._apply(
            [
                Consumption(
                    rule=rule, cost=self._estimate if rule.limit == "tokens_per_minute" else 1
                )
                for rule in rules
            ]
        )

    async def _apply(self, consumptions: list[Consumption]) -> None:
        if not consumptions:
            return
        outcome = await self._limiter.consume(consumptions, holder=self._holder)
        if outcome is None:
            self.degraded = True
            if self._limiter.fail_open:
                return
            raise RateLimitUnavailable(
                "Rate limits could not be checked, and this gateway is configured to "
                "refuse rather than serve traffic it cannot account for.",
            )

        self._readings.extend(outcome.readings)
        self._holds += outcome.holds
        refused = outcome.refused
        for reading in outcome.readings:
            self._limiter.observe(reading, rejected=reading is refused)
        if refused is not None:
            raise RateLimited(message(refused), retry_after_seconds=retry_after(refused))

    # -- afterwards ----------------------------------------------------------

    def headers(self) -> dict[str, str]:
        """``X-RateLimit-*`` for the tightest windowed limit seen so far.

        Called on the way out of *every* response, accepted or refused, because a client
        that only learns its budget when it has already exceeded it cannot pace itself —
        which is the whole reason the headers exist.
        """
        return headers(self.readings)

    async def release(self) -> None:
        """Give back the concurrency slot. Safe to call twice, and called from a
        ``finally`` that also runs on a client disconnect and on a stream that dies."""
        holds, self._holds = self._holds, ()
        await self._limiter.give_back(holds)

    async def settle(self, *, prompt_tokens: int | None, completion_tokens: int | None) -> None:
        """Correct the optimistic estimate with what the provider actually reported.

        Nothing to do when no token limit applied, when the provider reported no usage —
        which several do for streamed responses — or when the correction is zero. A
        provider that reports nothing leaves the estimate standing, which is the right
        failure: an unknown cost counted as the estimate is closer than an unknown cost
        counted as free.
        """
        if not self._token_rules:
            return
        actual = (prompt_tokens or 0) + (completion_tokens or 0)
        if actual <= 0:
            return
        delta = actual - self._estimate
        if delta == 0:
            return
        await self._limiter.settle_tokens([(rule, delta) for rule in self._token_rules])

    def _plan(self, names: tuple[str, ...]) -> tuple[Rule, ...]:
        return plan(
            self._limits,
            gateway_id=self._gateway_id,
            end_user_id=self._end_user_id,
            names=names,
        )


__all__ = ["RateLimiter", "RequestLimits"]
