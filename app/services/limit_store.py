"""The counters SPEC §11's limits are actually enforced against.

One port, two implementations, and the interesting half is a Lua script.

**Check-and-consume is one script, and it is all-or-nothing across every rule.** Not one
script per rule: a request refused by the per-end-user cap must not have already spent the
gateway's minute, and a read-modify-write split across commands leaks capacity under
exactly the concurrency this exists to survive. The script checks every rule first and
commits only if all of them passed, so a rejection costs nothing anywhere.

**The windows slide, as two weighted buckets.** A fixed window lets a client send twice
the limit across a boundary — 10 at 11:59:59 and 10 at 12:00:00 — which is the failure mode
the task calls out. The alternative most people reach for is a sorted set holding one
member per request, which is exact and costs memory proportional to the limit; at
``requests_per_day: 100000`` that is a hundred thousand members per gateway. This uses the
counter approximation instead: the previous bucket's count, weighted by how much of it is
still inside the trailing window, plus the current one. Two integers per rule, O(1)
memory, and the boundary burst it admits is bounded by the same weight it is measured
with.

The approximation is worth naming honestly: it assumes the previous window's requests were
spread evenly through it. A client that sent all of them in its final second is measured as
though it had not, so the true instantaneous rate can exceed the limit briefly. What it
cannot do is exceed it *sustainably*, which is what a rate limit is for.

**Concurrency is a set of holders with a lease, not a counter.** ``INCR`` on entry and
``DECR`` in a ``finally`` is one lost process away from a gateway that is throttled
forever, and the defensive ``EXPIRE`` usually suggested does not help: every new request
refreshes it, so a busy gateway's leaked counter never expires at all. A sorted set scored
by arrival time, pruned against a lease on every check, reclaims a dead holder's slot on
its own whether or not traffic continues. The set is bounded by the limit, which is a
small number by construction.

**The clock is the caller's, not Redis's.** ``now`` is passed in rather than read with
``TIME`` inside the script, so the bucket a request is counted in and the
``X-RateLimit-Reset`` it is told about are computed from one clock. Two clocks that agree
almost always are worse than one that is slightly wrong.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.services.limits import CONCURRENCY_LEASE_SECONDS, Reading, Rule

logger = logging.getLogger(__name__)

_WINDOW = "w"
_CONCURRENCY = "c"


CONSUME_SCRIPT = """
local now = tonumber(ARGV[1])
local count = tonumber(ARGV[2])
local kinds, keys, limits, spans, costs, members = {}, {}, {}, {}, {}, {}
local allowed, remaining, resets, slots = {}, {}, {}, {}
local refused = 0

for i = 1, count do
  local o = 2 + (i - 1) * 6
  kinds[i] = ARGV[o + 1]
  keys[i] = ARGV[o + 2]
  limits[i] = tonumber(ARGV[o + 3])
  spans[i] = tonumber(ARGV[o + 4])
  costs[i] = tonumber(ARGV[o + 5])
  members[i] = ARGV[o + 6]
end

for i = 1, count do
  local used = 0
  local reset = 0
  if kinds[i] == 'w' then
    local window = spans[i]
    local slot = math.floor(now / window)
    slots[i] = slot
    local elapsed = now - slot * window
    local current = tonumber(redis.call('GET', keys[i] .. ':' .. slot)) or 0
    local previous = tonumber(redis.call('GET', keys[i] .. ':' .. (slot - 1))) or 0
    used = previous * (1 - elapsed / window) + current
    reset = math.ceil(window - elapsed)
  else
    redis.call('ZREMRANGEBYSCORE', keys[i], '-inf', now - spans[i])
    used = redis.call('ZCARD', keys[i])
  end
  local projected = used + costs[i]
  local ok = projected <= limits[i]
  allowed[i] = ok and 1 or 0
  remaining[i] = math.max(0, math.floor(limits[i] - projected))
  resets[i] = reset
  if not ok and refused == 0 then refused = i end
end

if refused == 0 then
  for i = 1, count do
    if kinds[i] == 'w' then
      local key = keys[i] .. ':' .. slots[i]
      if costs[i] ~= 0 then
        redis.call('INCRBY', key, costs[i])
        -- Two windows, because the previous bucket is still being read and weighted for
        -- the whole of the current one.
        redis.call('EXPIRE', key, spans[i] * 2)
      end
    else
      redis.call('ZADD', keys[i], now, members[i])
      redis.call('EXPIRE', keys[i], spans[i] * 2)
    end
  end
end

local out = {refused}
for i = 1, count do
  out[#out + 1] = allowed[i]
  out[#out + 1] = remaining[i]
  out[#out + 1] = resets[i]
end
return out
"""

SETTLE_SCRIPT = """
local now = tonumber(ARGV[1])
local count = tonumber(ARGV[2])
for i = 1, count do
  local o = 2 + (i - 1) * 3
  local key = ARGV[o + 1] .. ':' .. math.floor(now / tonumber(ARGV[o + 2]))
  local value = redis.call('INCRBY', key, tonumber(ARGV[o + 3]))
  if value < 0 then
    redis.call('SET', key, 0)
  end
  redis.call('EXPIRE', key, tonumber(ARGV[o + 2]) * 2)
end
return 1
"""


@dataclass(frozen=True, slots=True)
class Consumption:
    """One rule and what this request wants to spend against it.

    ``cost`` is 1 for a request counter, 1 for a concurrency slot, and the estimated
    token count for a token limit — which is the only one that is a guess, and the only
    one :meth:`LimitStore.settle` later corrects.
    """

    rule: Rule
    cost: int = 1


@dataclass(frozen=True, slots=True)
class Hold:
    """A concurrency slot that has been taken and must be given back."""

    key: str
    member: str


@dataclass(frozen=True, slots=True)
class Consumed:
    """The whole answer: where every rule stands, and what has to be released."""

    readings: tuple[Reading, ...] = ()
    holds: tuple[Hold, ...] = ()

    @property
    def refused(self) -> Reading | None:
        for reading in self.readings:
            if not reading.allowed:
                return reading
        return None

    @property
    def allowed(self) -> bool:
        return self.refused is None


class LimitStore(Protocol):
    """Counters with windows, and slots with leases.

    Every method may raise: the caller's fail-open policy is what turns a Redis outage
    into a served request, and hiding it here would take that decision away from the one
    place the operator configured it.
    """

    async def consume(
        self, consumptions: Sequence[Consumption], *, holder: str, now: float | None = None
    ) -> Consumed:
        """Check every rule and commit all of them, or commit none."""

    async def release(self, holds: Sequence[Hold]) -> None:
        """Give concurrency slots back. Idempotent — a double release is a no-op."""

    async def settle(
        self, adjustments: Sequence[tuple[Rule, int]], *, now: float | None = None
    ) -> None:
        """Correct a token estimate by ``delta``, which may be negative.

        The correction lands in whichever bucket is current *now*, which is usually the
        one the estimate was taken from and is sometimes the next one. That is SPEC §11's
        "carried into the next window", and it falls out of the design rather than being
        arranged: a response that took ninety seconds to generate settles against the
        minute it finished in, which is the minute its tokens were actually produced.
        """

    async def peek(self, rules: Sequence[Rule], *, now: float | None = None) -> tuple[Reading, ...]:
        """Where each rule stands, consuming nothing. For the Limits screen."""


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


class RedisLimitStore:
    def __init__(self, redis: Any, *, lease_seconds: int = CONCURRENCY_LEASE_SECONDS) -> None:
        self._redis = redis
        self._lease = lease_seconds
        # Registered once so repeated calls are EVALSHA with a one-round-trip fallback,
        # rather than shipping the source of both scripts on every request.
        self._consume = redis.register_script(CONSUME_SCRIPT)
        self._settle = redis.register_script(SETTLE_SCRIPT)

    async def consume(
        self, consumptions: Sequence[Consumption], *, holder: str, now: float | None = None
    ) -> Consumed:
        if not consumptions:
            return Consumed()
        moment = now if now is not None else time.time()

        argv: list[Any] = [repr(moment), len(consumptions)]
        for item in consumptions:
            rule = item.rule
            argv.extend(
                [
                    _CONCURRENCY if rule.concurrency else _WINDOW,
                    rule.key,
                    rule.value,
                    self._lease if rule.concurrency else rule.window_seconds,
                    item.cost,
                    holder,
                ]
            )

        raw = await self._consume(keys=[], args=argv)
        return _decode(raw, consumptions, holder=holder)

    async def release(self, holds: Sequence[Hold]) -> None:
        if not holds:
            return
        async with self._redis.pipeline(transaction=False) as pipe:
            for hold in holds:
                pipe.zrem(hold.key, hold.member)
            await pipe.execute()

    async def settle(
        self, adjustments: Sequence[tuple[Rule, int]], *, now: float | None = None
    ) -> None:
        changes = [(rule, delta) for rule, delta in adjustments if delta and not rule.concurrency]
        if not changes:
            return
        moment = now if now is not None else time.time()
        argv: list[Any] = [repr(moment), len(changes)]
        for rule, delta in changes:
            argv.extend([rule.key, rule.window_seconds, delta])
        await self._settle(keys=[], args=argv)

    async def peek(self, rules: Sequence[Rule], *, now: float | None = None) -> tuple[Reading, ...]:
        if not rules:
            return ()
        moment = now if now is not None else time.time()

        async with self._redis.pipeline(transaction=False) as pipe:
            for rule in rules:
                if rule.concurrency:
                    pipe.zcount(rule.key, moment - self._lease, "+inf")
                else:
                    slot = math.floor(moment / rule.window_seconds)
                    pipe.get(f"{rule.key}:{slot}")
                    pipe.get(f"{rule.key}:{slot - 1}")
            values = await pipe.execute()

        readings: list[Reading] = []
        cursor = 0
        for rule in rules:
            if rule.concurrency:
                used, reset = float(values[cursor] or 0), 0
                cursor += 1
            else:
                current = float(values[cursor] or 0)
                previous = float(values[cursor + 1] or 0)
                cursor += 2
                used, reset = _weighted(previous, current, moment, rule.window_seconds)
            readings.append(_reading(rule, used, reset_seconds=reset))
        return tuple(readings)


def _decode(raw: Any, consumptions: Sequence[Consumption], *, holder: str) -> Consumed:
    """Turn the script's flat integer array back into readings and holds."""
    values = list(raw or [])
    refused = int(values[0]) if values else 0
    readings: list[Reading] = []
    holds: list[Hold] = []
    for index, item in enumerate(consumptions):
        offset = 1 + index * 3
        allowed = bool(int(values[offset]))
        readings.append(
            Reading(
                rule=item.rule,
                allowed=allowed,
                remaining=int(values[offset + 1]),
                reset_seconds=int(values[offset + 2]),
            )
        )
        if refused == 0 and item.rule.concurrency:
            holds.append(Hold(key=item.rule.key, member=holder))
    return Consumed(readings=tuple(readings), holds=tuple(holds))


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


@dataclass
class MemoryLimitStore:
    """The same arithmetic, in one process.

    Not correct across replicas, which is the whole reason :class:`RedisLimitStore`
    exists — but it is the same *decisions*, exercised by the same contract test, so a
    behaviour that only holds in one of the two shows up as a failure rather than as a
    surprise in production.
    """

    lease_seconds: int = CONCURRENCY_LEASE_SECONDS
    #: ``{key: {slot: count}}``. Expiry is not simulated: nothing here outlives a test,
    #: and a bucket two windows old is never read.
    counters: dict[str, dict[int, float]] = field(default_factory=dict)
    #: ``{key: {member: taken_at}}``.
    slots: dict[str, dict[str, float]] = field(default_factory=dict)

    async def consume(
        self, consumptions: Sequence[Consumption], *, holder: str, now: float | None = None
    ) -> Consumed:
        if not consumptions:
            return Consumed()
        moment = now if now is not None else time.time()

        readings: list[Reading] = []
        refused = False
        for item in consumptions:
            rule = item.rule
            if rule.concurrency:
                held = self._live(rule.key, moment)
                used, reset = float(len(held)), 0
            else:
                slot = math.floor(moment / rule.window_seconds)
                buckets = self.counters.setdefault(rule.key, {})
                used, reset = _weighted(
                    buckets.get(slot - 1, 0.0), buckets.get(slot, 0.0), moment, rule.window_seconds
                )
            projected = used + item.cost
            allowed = projected <= rule.value
            refused = refused or not allowed
            readings.append(
                Reading(
                    rule=rule,
                    allowed=allowed,
                    remaining=max(0, math.floor(rule.value - projected)),
                    reset_seconds=reset,
                )
            )

        holds: list[Hold] = []
        if not refused:
            for item in consumptions:
                rule = item.rule
                if rule.concurrency:
                    self.slots.setdefault(rule.key, {})[holder] = moment
                    holds.append(Hold(key=rule.key, member=holder))
                elif item.cost:
                    slot = math.floor(moment / rule.window_seconds)
                    buckets = self.counters.setdefault(rule.key, {})
                    buckets[slot] = buckets.get(slot, 0.0) + item.cost
        return Consumed(readings=tuple(readings), holds=tuple(holds))

    async def release(self, holds: Sequence[Hold]) -> None:
        for hold in holds:
            self.slots.get(hold.key, {}).pop(hold.member, None)

    async def settle(
        self, adjustments: Sequence[tuple[Rule, int]], *, now: float | None = None
    ) -> None:
        moment = now if now is not None else time.time()
        for rule, delta in adjustments:
            if not delta or rule.concurrency:
                continue
            slot = math.floor(moment / rule.window_seconds)
            buckets = self.counters.setdefault(rule.key, {})
            buckets[slot] = max(0.0, buckets.get(slot, 0.0) + delta)

    async def peek(self, rules: Sequence[Rule], *, now: float | None = None) -> tuple[Reading, ...]:
        moment = now if now is not None else time.time()
        readings: list[Reading] = []
        for rule in rules:
            if rule.concurrency:
                used, reset = float(len(self._live(rule.key, moment))), 0
            else:
                slot = math.floor(moment / rule.window_seconds)
                buckets = self.counters.get(rule.key, {})
                used, reset = _weighted(
                    buckets.get(slot - 1, 0.0), buckets.get(slot, 0.0), moment, rule.window_seconds
                )
            readings.append(_reading(rule, used, reset_seconds=reset))
        return tuple(readings)

    def _live(self, key: str, moment: float) -> dict[str, float]:
        """Prune expired leases, then report what is left — the same order the script
        uses, so a leaked slot is reclaimed by the next check either way."""
        holders = self.slots.setdefault(key, {})
        cutoff = moment - self.lease_seconds
        for member, taken_at in list(holders.items()):
            if taken_at < cutoff:
                del holders[member]
        return holders


# ---------------------------------------------------------------------------
# shared arithmetic
# ---------------------------------------------------------------------------


def _weighted(
    previous: float, current: float, moment: float, window_seconds: int
) -> tuple[float, int]:
    """``(used, seconds until this window rolls)`` for the sliding-window counter.

    The previous bucket contributes the fraction of it that is still inside the trailing
    window: at ten seconds into a minute, fifty seconds of the previous minute are still
    within the last sixty, so it counts for 5/6.
    """
    elapsed = moment - math.floor(moment / window_seconds) * window_seconds
    used = previous * (1 - elapsed / window_seconds) + current
    return used, max(0, math.ceil(window_seconds - elapsed))


def _reading(rule: Rule, used: float, *, reset_seconds: int = 0) -> Reading:
    """A reading of the current state, with nothing consumed. ``allowed`` here means
    "there is room", which is what the Limits screen is asking."""
    return Reading(
        rule=rule,
        allowed=used < rule.value,
        remaining=max(0, math.floor(rule.value - used)),
        reset_seconds=reset_seconds,
    )


__all__ = [
    "CONSUME_SCRIPT",
    "SETTLE_SCRIPT",
    "Consumed",
    "Consumption",
    "Hold",
    "LimitStore",
    "MemoryLimitStore",
    "RedisLimitStore",
]
