"""What a gateway's limits mean, with no I/O in sight.

SPEC §11 gives four caps at two scopes. Everything that decides *which* of those eight
numbers applies to a request, in what order they are checked, what a 429 says and what the
``X-RateLimit-*`` headers claim, lives here — as functions over frozen values. The Redis
side is :mod:`app.services.limit_store`; the orchestration is
:mod:`app.services.limiter`.

Four decisions are worth reading before the code.

**Cheapest first, and the order is a cost order, not a priority.** Request counters are
two integers; token limits need the assembled prompt measured; a concurrency slot is the
only one of the four that has to be handed back afterwards. So they are checked in that
order, and a request refused by the cheapest one never pays for the others. It also makes
the 429 deterministic: when three limits are exceeded at once, the message names the same
one every time.

**The ceiling is a maximum, not a default.** A gateway routing to a global catalog model
is spending the *operator's* credential (SPEC §8.4, §17.3), so the platform sets a
maximum that an org_admin cannot raise. A gateway that asked for less keeps its own
number — a ceiling that also raised limits would silently loosen every careful
configuration on the platform the day the operator set one.

**Unlimited is a real answer, and it is the default.** ``None`` everywhere means this
returns an empty plan, and an empty plan means the request path does not talk to Redis at
all. That is what keeps rate limiting off the latency budget of every gateway that has
not configured it.

**The headers describe the tightest limit, and only a windowed one.** A client can
self-pace against one number; four sets of headers is a specification, not a courtesy.
Concurrency is excluded because ``X-RateLimit-Reset`` would be a lie — a slot frees when
some other request finishes, not at a time anybody can name.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Literal

from app.schemas.gateway_config import LIMIT_NAMES, LimitsConfig, Quota

#: SPEC §11 keys buckets by ``(gateway_id)`` and ``(gateway_id, end_user_id)``. The limit
#: name is the third segment rather than the window, because two limits share the minute
#: window and a key that named only the window would have them spending each other's
#: budget.
KEY_PREFIX = "rl"

MINUTE_SECONDS = 60
DAY_SECONDS = 24 * 3600

#: How long a concurrency slot may be held before the limiter assumes its holder died.
#: Generous against the routing deadline, because the cost of guessing wrong in one
#: direction is a request that briefly over-admits and in the other is a gateway that is
#: throttled forever by a counter nobody can find. Task 14's own note says the second is
#: the failure to design against.
CONCURRENCY_LEASE_SECONDS = 900

#: Above this fraction of a limit, a gateway is "near limit" — the dashboard warning and
#: the near-limit metric. Chosen because it is far enough below 1.0 to be a warning
#: rather than a report of something that already happened.
NEAR_LIMIT = 0.8

type Scope = Literal["gateway", "end_user"]

#: Human words for the scopes, for the one place they are read by a person: the 429.
SCOPE_LABELS: dict[str, str] = {"gateway": "this gateway", "end_user": "this end user"}

LIMIT_LABELS: dict[str, str] = {
    "requests_per_minute": "requests per minute",
    "requests_per_day": "requests per day",
    "tokens_per_minute": "tokens per minute",
    "concurrent_requests": "concurrent requests",
}

#: Which limits are checked before anything expensive happens, and which are checked
#: immediately before dispatch. The split is the whole of "evaluate cheapest-first": the
#: early phase needs nothing but the gateway and who is asking, while the late phase needs
#: the assembled prompt — retrieval, injected memory and all — to be measured first.
EARLY_LIMITS: tuple[str, ...] = ("requests_per_minute", "requests_per_day")
LATE_LIMITS: tuple[str, ...] = ("tokens_per_minute", "concurrent_requests")

WINDOWS: dict[str, int] = {
    "requests_per_minute": MINUTE_SECONDS,
    "requests_per_day": DAY_SECONDS,
    "tokens_per_minute": MINUTE_SECONDS,
    #: Not a window at all. Zero is the marker the store reads to switch algorithms, and
    #: it is checked rather than inferred from the name.
    "concurrent_requests": 0,
}


# ---------------------------------------------------------------------------
# the ceiling
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ceilings:
    """The platform's maxima for a gateway on the operator's credential.

    ``None`` means the operator has set no ceiling for that limit, which is the default
    and is not the same as zero.
    """

    requests_per_minute: int | None = None
    requests_per_day: int | None = None
    tokens_per_minute: int | None = None
    concurrent_requests: int | None = None

    @classmethod
    def of(cls, settings: object) -> Ceilings:
        """Read the four ``global_model_*`` settings, by name rather than by import.

        Duck-typed on purpose: this module is pure, and importing ``Settings`` would drag
        pydantic-settings and a ``.env`` file into every unit test of a ratio.
        """
        return cls(
            requests_per_minute=getattr(settings, "global_model_requests_per_minute", None),
            requests_per_day=getattr(settings, "global_model_requests_per_day", None),
            tokens_per_minute=getattr(settings, "global_model_tokens_per_minute", None),
            concurrent_requests=getattr(settings, "global_model_concurrent_requests", None),
        )

    @property
    def empty(self) -> bool:
        return all(getattr(self, name) is None for name in LIMIT_NAMES)

    def cap(self, name: str, value: int | None) -> int | None:
        """The stricter of a configured value and the ceiling for ``name``.

        ``None`` on either side is unlimited, so the ceiling wins against an unconfigured
        limit and the configured value wins against an unset ceiling. That asymmetry is
        the point: switching a gateway to a global model must not leave it unbounded.
        """
        ceiling = getattr(self, name, None)
        if ceiling is None:
            return value
        if value is None:
            return int(ceiling)
        return min(int(value), int(ceiling))


#: "the operator has set no ceilings", as a value. A module-level singleton rather than a
#: default constructed per call, which is both cheaper and the only shape ruff will accept
#: in a signature.
NO_CEILINGS = Ceilings()


@dataclass(frozen=True, slots=True)
class Effective:
    """What will actually be enforced, and what the ceiling changed on the way here."""

    gateway: Quota
    per_end_user: Quota
    #: Gateway-scope limits the ceiling lowered or introduced. Carried so the editor can
    #: say *why* the number it is showing is not the number that was typed, which is the
    #: difference between a platform policy and a bug report.
    capped: tuple[str, ...] = ()

    @property
    def unlimited(self) -> bool:
        return self.gateway.unlimited and self.per_end_user.unlimited


def effective(
    limits: LimitsConfig,
    *,
    ceilings: Ceilings = NO_CEILINGS,
    global_models: bool = False,
) -> Effective:
    """Apply the platform ceiling to a gateway's configured limits.

    ``global_models`` is whether this gateway routes to at least one global catalog
    model. It is a property of the routing chain, not of the limits blob, which is why
    the ceiling cannot be baked into the stored configuration: repointing a gateway at a
    global model has to start enforcing it on the next request, without anybody
    re-saving the Limits section.
    """
    if not global_models or ceilings.empty:
        return Effective(gateway=limits.gateway, per_end_user=limits.per_end_user)

    configured = limits.gateway
    values = {name: ceilings.cap(name, getattr(configured, name)) for name in LIMIT_NAMES}
    capped = tuple(name for name in LIMIT_NAMES if values[name] != getattr(configured, name))
    return Effective(
        gateway=Quota(**values),
        per_end_user=limits.per_end_user,
        capped=capped,
    )


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rule:
    """One cap, resolved against one request: which bucket, how big, how long."""

    scope: Scope
    limit: str
    value: int
    key: str
    #: Seconds the bucket spans, or ``0`` for concurrency — which is a set of holders
    #: rather than a count over time.
    window_seconds: int

    @property
    def concurrency(self) -> bool:
        return self.window_seconds == 0

    def describe(self) -> str:
        """The sentence a 429 is built from. Names the cap *and* the scope, because
        "10 requests per minute" is a different problem to solve depending on whether it
        is the endpoint's budget or one caller's."""
        return (
            f"{self.value} {LIMIT_LABELS.get(self.limit, self.limit)} "
            f"for {SCOPE_LABELS.get(self.scope, self.scope)}"
        )


def bucket_key(
    limit: str,
    *,
    gateway_id: uuid.UUID,
    end_user_id: uuid.UUID | None = None,
) -> str:
    """``rl:{gateway_id}:{limit}`` or ``rl:{gateway_id}:{end_user_id}:{limit}``.

    The end-user segment is the *resolved row id*, never the caller-supplied
    ``X-Gateway-User`` string. Two reasons, and the second is the one that matters: an
    external id can be an email address, and a key space full of them is a mailing list
    sitting in a cache nobody thinks of as a data store.
    """
    if end_user_id is None:
        return f"{KEY_PREFIX}:{gateway_id}:{limit}"
    return f"{KEY_PREFIX}:{gateway_id}:{end_user_id}:{limit}"


def plan(
    limits: Effective,
    *,
    gateway_id: uuid.UUID,
    end_user_id: uuid.UUID | None,
    names: tuple[str, ...],
) -> tuple[Rule, ...]:
    """The rules to check for this request, in cost order.

    ``names`` selects the phase — :data:`EARLY_LIMITS` before any work has been done,
    :data:`LATE_LIMITS` once the prompt exists. Within a phase the gateway's own cap comes
    before the per-end-user one, so an endpoint that is over budget as a whole says so
    rather than blaming whichever caller happened to arrive.

    An unlimited gateway produces an empty tuple, and an empty tuple never reaches Redis.
    """
    rules: list[Rule] = []
    for name in names:
        window = WINDOWS[name]
        gateway_value = getattr(limits.gateway, name)
        if gateway_value is not None:
            rules.append(
                Rule(
                    scope="gateway",
                    limit=name,
                    value=int(gateway_value),
                    key=bucket_key(name, gateway_id=gateway_id),
                    window_seconds=window,
                )
            )
        end_user_value = getattr(limits.per_end_user, name)
        # Skipped entirely when nobody identified the caller: SPEC §6.2's anonymous case
        # would otherwise put every unidentified request in one bucket and throttle them
        # as though they were the same person.
        if end_user_value is not None and end_user_id is not None:
            rules.append(
                Rule(
                    scope="end_user",
                    limit=name,
                    value=int(end_user_value),
                    key=bucket_key(name, gateway_id=gateway_id, end_user_id=end_user_id),
                    window_seconds=window,
                )
            )
    return tuple(rules)


# ---------------------------------------------------------------------------
# readings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reading:
    """Where one rule stood after this request was weighed against it."""

    rule: Rule
    allowed: bool
    #: What is left of the cap once this request is counted. Zero for a rejection, which
    #: is the honest answer: the budget is gone either way.
    remaining: int
    #: Seconds until the window this reading was taken in rolls over. ``0`` for
    #: concurrency — see the module docstring.
    reset_seconds: int

    @property
    def used(self) -> int:
        return max(0, self.rule.value - self.remaining)

    @property
    def utilization(self) -> float:
        return min(1.0, self.used / self.rule.value) if self.rule.value else 0.0

    @property
    def near_limit(self) -> bool:
        return self.utilization >= NEAR_LIMIT


def tightest(readings: tuple[Reading, ...]) -> Reading | None:
    """The reading a client should pace itself against.

    The least *fractional* headroom rather than the smallest absolute remainder: 900 of
    1000 tokens left is not tighter than 3 of 10 requests, and a client that slowed down
    for the first number would be reacting to the wrong limit.

    Concurrency is not a candidate. A window it does not have cannot be reported in a
    header whose whole content is when the window ends.
    """
    windowed = [reading for reading in readings if not reading.rule.concurrency]
    if not windowed:
        return None
    return min(
        windowed,
        # The tie-break is the shorter window, so a minute limit is reported ahead of a
        # daily one at equal pressure — it is the one about to bite.
        key=lambda reading: (reading.remaining / reading.rule.value, reading.rule.window_seconds),
    )


def headers(readings: tuple[Reading, ...]) -> dict[str, str]:
    """``X-RateLimit-*`` for the tightest windowed rule, or nothing at all.

    Absent rather than zero-valued when a gateway has no limits: a client reading
    ``X-RateLimit-Remaining: 0`` on an unlimited endpoint would back off forever.
    """
    reading = tightest(readings)
    if reading is None:
        return {}
    return {
        "X-RateLimit-Limit": str(reading.rule.value),
        "X-RateLimit-Remaining": str(max(0, reading.remaining)),
        "X-RateLimit-Reset": str(max(0, reading.reset_seconds)),
    }


def retry_after(reading: Reading) -> int:
    """Seconds a rejected caller should wait, never zero.

    For a windowed rule, until the window rolls. For concurrency there is no such moment,
    so it is one second — a slot frees when some other request finishes, and telling a
    client to wait a minute for something that is usually over in two hundred
    milliseconds turns a brief queue into an outage.
    """
    if reading.rule.concurrency:
        return 1
    return max(1, math.ceil(reading.reset_seconds))


def message(reading: Reading) -> str:
    """The 429's text: which limit, whose, and what to do about it."""
    return f"Rate limit exceeded: {reading.rule.describe()}. Retry after {retry_after(reading)}s."


__all__ = [
    "CONCURRENCY_LEASE_SECONDS",
    "DAY_SECONDS",
    "EARLY_LIMITS",
    "KEY_PREFIX",
    "LATE_LIMITS",
    "LIMIT_LABELS",
    "MINUTE_SECONDS",
    "NEAR_LIMIT",
    "NO_CEILINGS",
    "WINDOWS",
    "Ceilings",
    "Effective",
    "Reading",
    "Rule",
    "Scope",
    "bucket_key",
    "effective",
    "headers",
    "message",
    "plan",
    "retry_after",
    "tightest",
]
