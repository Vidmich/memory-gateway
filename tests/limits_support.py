"""Test doubles for SPEC §11's rate limiting.

The store is in-memory and the limiter is real. That split is deliberate: everything worth
testing about this feature — exactly N admitted under concurrent load, a burst straddling a
window boundary, an optimistic estimate settled against real usage, a slot released when a
client hangs up — is arithmetic and ordering, and none of it needs a socket. What the
in-memory store *cannot* prove is that the Lua script does the same arithmetic atomically;
that is what :mod:`tests.limit_store_contract` is for, run against both implementations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from prometheus_client import CollectorRegistry

from app.core.metrics import RateLimitMetrics, build_rate_limit_metrics
from app.schemas.gateway_config import LimitsConfig, Quota
from app.services.limit_store import Consumed, Consumption, Hold, MemoryLimitStore
from app.services.limiter import RateLimiter
from app.services.limits import Ceilings, Reading, Rule


@dataclass
class LimitFixture:
    """A limiter with its counters in reach, so a test can look at both sides."""

    #: Whatever the limiter was built over — usually :class:`MemoryLimitStore`, sometimes
    #: one of the doubles below. Loosely typed on purpose: a test that swapped in the
    #: broken store wants to read *its* fields, not the memory store's.
    store: Any
    limiter: RateLimiter
    metrics: RateLimitMetrics
    registry: CollectorRegistry

    def count(self, name: str, **labels: str) -> float:
        """One counter's value, or 0.0 when nothing has incremented it yet."""
        value = self.registry.get_sample_value(name, labels or None)
        return float(value or 0.0)


def build_limits(
    *,
    ceilings: Ceilings | None = None,
    fail_open: bool = True,
    store: Any | None = None,
) -> LimitFixture:
    registry = CollectorRegistry()
    metrics = build_rate_limit_metrics(registry)
    buckets = store if store is not None else MemoryLimitStore()
    return LimitFixture(
        store=buckets,
        limiter=RateLimiter(
            buckets,
            ceilings=ceilings or Ceilings(),
            fail_open=fail_open,
            metrics=metrics,
        ),
        metrics=metrics,
        registry=registry,
    )


def limits_config(
    *, per_end_user: dict[str, int | None] | None = None, **caps: int
) -> LimitsConfig:
    """A ``LimitsConfig`` from the two or three numbers a test actually cares about."""
    return LimitsConfig(**caps, per_end_user=Quota(**(per_end_user or {})))


@dataclass
class BrokenLimitStore:
    """Every operation raises, as a refused Redis connection does.

    Reads and writes alike: a fail-open test that only broke the write half would still be
    talking to a working store, and the outage being simulated does not work that way.
    """

    calls: list[str] = field(default_factory=list)

    async def consume(
        self, consumptions: Any, *, holder: str, now: float | None = None
    ) -> Consumed:
        self.calls.append("consume")
        raise ConnectionError("redis is down")

    async def release(self, holds: Any) -> None:
        self.calls.append("release")
        raise ConnectionError("redis is down")

    async def settle(self, adjustments: Any, *, now: float | None = None) -> None:
        self.calls.append("settle")
        raise ConnectionError("redis is down")

    async def peek(self, rules: Any, *, now: float | None = None) -> tuple[Reading, ...]:
        self.calls.append("peek")
        raise ConnectionError("redis is down")


@dataclass
class RecordingLimitStore:
    """Wraps a real store and remembers what it was asked, in order.

    For the assertions that are about *when* a check happened rather than about its
    result — that a request refused on the cheap phase never reached the expensive one, for
    instance, which is invisible from the outcome alone.
    """

    inner: MemoryLimitStore = field(default_factory=MemoryLimitStore)
    consumed: list[tuple[Consumption, ...]] = field(default_factory=list)
    released: list[tuple[Hold, ...]] = field(default_factory=list)
    settled: list[tuple[tuple[Rule, int], ...]] = field(default_factory=list)

    async def consume(
        self, consumptions: Any, *, holder: str, now: float | None = None
    ) -> Consumed:
        self.consumed.append(tuple(consumptions))
        return await self.inner.consume(consumptions, holder=holder, now=now)

    async def release(self, holds: Any) -> None:
        self.released.append(tuple(holds))
        await self.inner.release(holds)

    async def settle(self, adjustments: Any, *, now: float | None = None) -> None:
        self.settled.append(tuple(adjustments))
        await self.inner.settle(adjustments, now=now)

    async def peek(self, rules: Any, *, now: float | None = None) -> tuple[Reading, ...]:
        return await self.inner.peek(rules, now=now)

    @property
    def limits_checked(self) -> list[str]:
        """Every limit name that was actually weighed, in the order it was."""
        return [item.rule.limit for batch in self.consumed for item in batch]


__all__ = [
    "BrokenLimitStore",
    "LimitFixture",
    "RecordingLimitStore",
    "build_limits",
    "limits_config",
]
