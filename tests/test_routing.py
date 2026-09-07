"""Routing policy: what gets tried, in what order, and what stops the chain.

Everything here is in memory and deterministic. The proxy is a fake that answers from a
script, so a failover chain runs in microseconds and a "provider" can be made to return
exactly the status the SPEC §8.2 table names. The end-to-end proof — real sockets, real
SSE, a real request log row — is ``tests/test_proxy_failover.py``; this file is about the
decisions, and it is the one that can afford to enumerate all eleven status codes.

The backoff is stubbed to zero in every test but the one that is about backoff. A jitter
of 50-150 ms per attempt is protection for a provider under load, not behaviour a unit
test should sit and wait for.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections import Counter
from dataclasses import replace
from typing import Any

import pytest

from app.adapters.base import UpstreamTarget
from app.api.proxy.errors import (
    GatewayUnavailable,
    InvalidRequest,
    ProxyError,
    UpstreamStatus,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from app.core.ids import uuid7
from app.schemas.openai import ChatMessage, ChatRequest, ChatResponse
from app.services.gateway_resolver import ResolvedGateway
from app.services.params import Resolved
from app.services.proxy import Prepared
from app.services.retrieval import Recall
from app.services.routing import (
    RETRY_TABLE,
    Attempts,
    Router,
    is_retryable,
    plan,
    select,
)
from tests.support import completion, make_target

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# doubles
# ---------------------------------------------------------------------------


class ScriptedProxy:
    """A proxy whose every call is answered from a per-model script.

    Deliberately not a mock library: what these tests assert on is *which models were
    called and in what order*, and a list of names is a clearer statement of that than a
    call-args matcher.
    """

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        #: model name -> either an exception to raise or a ChatResponse to return.
        self.answers: dict[str, Any] = answers or {}
        self.called: list[str] = []
        self.delays: dict[str, float] = {}
        self.recalls: list[Recall | None] = []

    def prepare(
        self,
        request: ChatRequest,
        gateway: ResolvedGateway,
        target: UpstreamTarget,
        *,
        recall: Recall | None = None,
    ) -> Prepared:
        # `recall` is accepted and ignored: what these tests assert on is which targets
        # were called, and prompt assembly has its own file. That it is threaded through
        # per attempt at all is asserted by
        # `test_the_same_retrieval_is_assembled_into_every_attempt`.
        self.recalls.append(recall)
        return Prepared(request=request, params=Resolved(values={}), target=target)

    async def complete(self, prepared: Prepared) -> ChatResponse:
        name = prepared.target.name
        self.called.append(name)
        delay = self.delays.get(name)
        if delay:
            await asyncio.sleep(delay)
        answer = self.answers.get(name)
        if isinstance(answer, BaseException):
            raise answer
        return answer or ChatResponse.model_validate(completion(f"from {name}"))

    async def open_stream(self, prepared: Prepared, *, observer: Any = None) -> Any:
        return await self.complete(prepared)


def target(name: str, **overrides: Any) -> UpstreamTarget:
    return make_target("http://example.invalid/v1", name=name, id=uuid7(), **overrides)


def gateway(*targets: UpstreamTarget, mode: str = "single", **overrides: Any) -> ResolvedGateway:
    values: dict[str, Any] = {
        "id": uuid7(),
        "organization_id": uuid7(),
        "slug": "demo",
        "name": "Demo",
        "routing_mode": mode,
        "targets": targets,
    }
    values.update(overrides)
    return ResolvedGateway(**values)


def request() -> ChatRequest:
    return ChatRequest(model="demo", messages=[ChatMessage(role="user", content="hi")])


@pytest.fixture
def pauses(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Records what the router asked to sleep for, without sleeping.

    The pause is the one thing here that is genuinely about wall clock, and asserting on
    the *requested* duration is both faster and stricter than measuring elapsed time on a
    laptop whose clock has 15 ms of granularity.
    """
    recorded: list[float] = []

    async def sleep(seconds: float, *args: Any, **kwargs: Any) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return recorded


def router(proxy: ScriptedProxy, **overrides: Any) -> Router:
    options: dict[str, Any] = {"backoff": lambda: 0.0}
    options.update(overrides)
    return Router(proxy, **options)  # type: ignore[arg-type]


def status(code: int) -> ProxyError:
    return UpstreamStatus(status_code=code, model_name="x", message="nope")


async def run(
    routing_gateway: ResolvedGateway, proxy: ScriptedProxy, **overrides: Any
) -> tuple[Any, Attempts]:
    attempts = Attempts()
    result = await router(proxy, **overrides).complete(
        request(), routing_gateway, plan(routing_gateway), attempts
    )
    return result, attempts


# ---------------------------------------------------------------------------
# retry classification (SPEC §8.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("code", "expected"), sorted(RETRY_TABLE.items()))
def test_every_status_in_the_table_is_classified_as_the_spec_says(
    code: int, expected: bool
) -> None:
    assert is_retryable(status(code)) is expected


def test_the_table_covers_exactly_the_codes_the_spec_names() -> None:
    """A guard on the table itself. Someone adding 409 here should have to say so."""
    assert sorted(code for code, retry in RETRY_TABLE.items() if retry) == [
        408,
        429,
        500,
        502,
        503,
        504,
    ]
    assert sorted(code for code, retry in RETRY_TABLE.items() if not retry) == [
        400,
        401,
        403,
        404,
        422,
    ]


def test_a_read_timeout_is_retryable_because_the_proxy_calls_it_a_504() -> None:
    """Transport failures never reach the classifier as transport errors: the proxy has
    already turned them into statuses, which is what keeps the table complete."""
    assert is_retryable(UpstreamTimeout("timed out")) is True
    assert is_retryable(UpstreamUnavailable("unreachable")) is True


def test_a_gateway_misconfiguration_is_retryable() -> None:
    """A credential that will not decrypt is this target's problem, not the request's —
    the next target has its own."""
    assert is_retryable(GatewayUnavailable("bad credential")) is True


def test_an_unknown_status_is_not_retried() -> None:
    """A failure nobody classified is not evidence that trying again will help, and
    guessing costs the caller another provider round trip."""
    assert is_retryable(status(418)) is False


def test_a_bug_in_this_process_is_not_retried() -> None:
    assert is_retryable(RuntimeError("boom")) is False


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


def test_single_mode_plans_one_attempt_even_with_a_longer_chain() -> None:
    """Save-time validation refuses this shape, but a row written around the API must not
    turn into a fan-out."""
    first, second = target("a"), target("b")
    routing = plan(gateway(first, second, mode="single"))

    assert [step.name for step in routing.targets] == ["a"]
    assert routing.can_retry is False


def test_failover_plans_the_whole_chain_in_priority_order() -> None:
    routing = plan(gateway(target("a"), target("b"), target("c"), mode="failover"))

    assert [step.name for step in routing.targets] == ["a", "b", "c"]
    assert routing.can_retry is True


def test_ab_split_plans_exactly_one_attempt() -> None:
    """SPEC §8.1: a failure is returned to the client. A retried A/B request would land
    on the other variant and corrupt the comparison the mode exists for."""
    first, second = target("a"), target("b")
    routing = plan(
        gateway(first, second, mode="ab_split", weights={first.id: 50, second.id: 50}),
        end_user_key="u1",
    )

    assert len(routing.targets) == 1
    assert routing.can_retry is False


def test_an_unknown_mode_degrades_to_one_target() -> None:
    routing = plan(gateway(target("a"), target("b"), mode="round_robin"))

    assert [step.name for step in routing.targets] == ["a"]


def test_a_gateway_with_no_targets_says_what_to_fix() -> None:
    with pytest.raises(GatewayUnavailable) as raised:
        plan(gateway())

    assert "no upstream model configured" in raised.value.message


def test_a_gateway_whose_models_are_all_disabled_says_which() -> None:
    with pytest.raises(GatewayUnavailable) as raised:
        plan(gateway(disabled=("acme-gpt",)))

    assert "acme-gpt" in raised.value.message


# ---------------------------------------------------------------------------
# weighted selection
# ---------------------------------------------------------------------------


def bands(a: UpstreamTarget, b: UpstreamTarget, split: tuple[int, int]) -> dict[uuid.UUID, int]:
    return {a.id: split[0], b.id: split[1]}


def test_the_same_user_lands_on_the_same_target_every_time() -> None:
    a, b = target("a"), target("b")
    gateway_id = uuid7()

    picks = {
        select([a, b], bands(a, b, (70, 30)), gateway_id=gateway_id, key="user-1").name
        for _ in range(100)
    }

    assert len(picks) == 1


def test_without_a_user_selection_varies() -> None:
    a, b = target("a"), target("b")
    weights = bands(a, b, (50, 50))
    gateway_id = uuid7()

    picks = Counter(
        select([a, b], weights, gateway_id=gateway_id, key=None).name for _ in range(400)
    )

    assert set(picks) == {"a", "b"}


def test_the_same_user_can_land_differently_on_a_different_gateway() -> None:
    """The gateway id is in the hash so that somebody in the bottom band is not in the
    bottom band of every experiment in the account."""
    a, b = target("a"), target("b")
    weights = bands(a, b, (50, 50))

    picks = {select([a, b], weights, gateway_id=uuid7(), key="user-1").name for _ in range(200)}

    assert picks == {"a", "b"}


def test_a_seventy_thirty_split_lands_within_three_points() -> None:
    """The acceptance criterion, over synthetic user ids rather than over coin flips:
    stickiness means the distribution is a property of the id space, not of chance."""
    a, b = target("a"), target("b")
    weights = bands(a, b, (70, 30))
    gateway_id = uuid7()

    picks = Counter(
        select([a, b], weights, gateway_id=gateway_id, key=f"user-{index}").name
        for index in range(2000)
    )

    assert abs(picks["a"] / 20 - 70) <= 3


def test_a_thousand_anonymous_requests_land_within_three_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance criterion for the anonymous path, made deterministic.

    A thousand uniform draws at 70/30 has a standard deviation of about 1.4 points, so an
    unseeded version of this would fail roughly one run in twenty — and a flaky test about
    a statistical property is worse than no test, because it trains people to re-run it.
    Seeding fixes the sample; what is being checked is that the *bands* are right, and the
    sticky test above covers the hashing.
    """
    monkeypatch.setattr(random, "randrange", random.Random(20260907).randrange)
    a, b = target("a"), target("b")
    weights = bands(a, b, (70, 30))
    gateway_id = uuid7()

    picks = Counter(
        select([a, b], weights, gateway_id=gateway_id, key=None).name for _ in range(1000)
    )

    assert abs(picks["a"] / 10 - 70) <= 3


def test_a_zero_weight_never_wins() -> None:
    a, b = target("a"), target("b")
    weights = bands(a, b, (100, 0))

    picks = {
        select([a, b], weights, gateway_id=uuid7(), key=f"user-{index}").name
        for index in range(200)
    }

    assert picks == {"a"}


def test_weights_that_do_not_total_a_hundred_still_split_in_proportion() -> None:
    """Save-time validation is what keeps this from happening; dividing by the real total
    is what keeps a chain written around the API — or one a disabled model dropped out
    of — from sending a slice of traffic nowhere."""
    a, b = target("a"), target("b")
    weights = bands(a, b, (30, 10))
    gateway_id = uuid7()

    picks = Counter(
        select([a, b], weights, gateway_id=gateway_id, key=f"user-{index}").name
        for index in range(2000)
    )

    assert abs(picks["a"] / 20 - 75) <= 3


def test_all_weights_zero_falls_back_to_uniform_rather_than_refusing_to_serve() -> None:
    a, b = target("a"), target("b")

    picks = {
        select([a, b], bands(a, b, (0, 0)), gateway_id=uuid7(), key="u").name for _ in range(200)
    }

    assert picks == {"a", "b"}


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------


async def test_a_broken_primary_is_answered_by_the_secondary() -> None:
    a, b = target("a"), target("b")
    proxy = ScriptedProxy({"a": status(503)})

    result, attempts = await run(gateway(a, b, mode="failover"), proxy)

    assert proxy.called == ["a", "b"]
    assert result.prepared.target.name == "b"
    assert [record.status for record in attempts.records] == [503, 200]


async def test_a_working_primary_means_the_secondary_is_never_called() -> None:
    a, b = target("a"), target("b")
    proxy = ScriptedProxy()

    await run(gateway(a, b, mode="failover"), proxy)

    assert proxy.called == ["a"]


async def test_when_every_target_fails_the_client_gets_the_last_error() -> None:
    a, b = target("a"), target("b")
    proxy = ScriptedProxy({"a": status(503), "b": status(429)})

    with pytest.raises(UpstreamStatus) as raised:
        await run(gateway(a, b, mode="failover"), proxy)

    assert raised.value.status_code == 429
    assert proxy.called == ["a", "b"]


async def test_a_four_hundred_is_returned_without_trying_the_secondary() -> None:
    """The next target would reject it identically, so trying is one bad request turned
    into two — and a second lot of latency for a caller who already made a mistake."""
    a, b = target("a"), target("b")
    proxy = ScriptedProxy({"a": InvalidRequest("bad body")})

    with pytest.raises(InvalidRequest):
        await run(gateway(a, b, mode="failover"), proxy)

    assert proxy.called == ["a"]


async def test_ab_split_does_not_retry() -> None:
    a, b = target("a"), target("b")
    proxy = ScriptedProxy({"a": status(503), "b": status(503)})
    routed = gateway(a, b, mode="ab_split", weights={a.id: 100, b.id: 0})

    with pytest.raises(UpstreamStatus):
        await run(routed, proxy)

    assert proxy.called == ["a"]


async def test_the_last_attempt_records_that_it_was_retryable_anyway() -> None:
    """ "The chain was too short" and "the error was final" are different problems, and
    only the flag on the last attempt tells them apart."""
    a, b = target("a"), target("b")
    proxy = ScriptedProxy({"a": status(503), "b": status(503)})

    attempts = Attempts()
    with pytest.raises(UpstreamStatus):
        routed = gateway(a, b, mode="failover")
        await router(proxy).complete(request(), routed, plan(routed), attempts)

    assert [record.retryable for record in attempts.records] == [True, True]


# ---------------------------------------------------------------------------
# what the log gets
# ---------------------------------------------------------------------------


async def test_one_clean_attempt_is_recorded_as_no_attempts_at_all() -> None:
    """The row's own model, status and latency columns already say everything a
    one-element array would. Non-empty means "more than one target was involved"."""
    proxy = ScriptedProxy()

    _, attempts = await run(gateway(target("a")), proxy)

    assert len(attempts) == 1
    assert attempts.as_json() == []


async def test_a_failover_writes_every_attempt_in_order() -> None:
    a, b = target("a"), target("b")
    proxy = ScriptedProxy({"a": status(503)})

    _, attempts = await run(gateway(a, b, mode="failover"), proxy)
    records = attempts.as_json()

    assert [record["model_name"] for record in records] == ["a", "b"]
    assert [record["status"] for record in records] == [503, 200]
    assert [record["error_code"] for record in records] == ["upstream_error", None]
    assert records[0]["target_id"] == str(a.id)
    assert all(isinstance(record["latency_ms"], int) for record in records)


async def test_a_failed_chain_still_has_its_history() -> None:
    """The whole reason the caller owns the attempt list: this path raises, and this is
    exactly the request somebody will open the drawer for."""
    a, b = target("a"), target("b")
    proxy = ScriptedProxy({"a": status(503), "b": status(500)})
    attempts = Attempts()

    with pytest.raises(UpstreamStatus):
        routed = gateway(a, b, mode="failover")
        await router(proxy).complete(request(), routed, plan(routed), attempts)

    assert [record["status"] for record in attempts.as_json()] == [503, 500]


async def test_the_prompt_of_each_attempt_is_reported_as_it_is_prepared() -> None:
    """Two targets can carry different system contexts, so the transcript has to follow
    the chain rather than be captured once up front."""
    a, b = target("a"), target("b")
    seen: list[str] = []
    attempts = Attempts(on_prepared=lambda prepared: seen.append(prepared.target.name))
    proxy = ScriptedProxy({"a": status(503)})

    routed = gateway(a, b, mode="failover")
    await router(proxy).complete(request(), routed, plan(routed), attempts)

    assert seen == ["a", "b"]


# ---------------------------------------------------------------------------
# the deadline
# ---------------------------------------------------------------------------


async def test_the_overall_deadline_stops_a_chain_of_slow_targets() -> None:
    """Three targets at their own timeouts is three timeouts long. The deadline is what
    stops the chain, and it applies *inside* an attempt rather than only between them."""
    a, b, c = target("a"), target("b"), target("c")
    proxy = ScriptedProxy({"a": status(503)})
    proxy.delays = {"b": 5.0, "c": 5.0}

    routed = gateway(a, b, c, mode="failover")
    attempts = Attempts()
    with pytest.raises(UpstreamTimeout) as raised:
        await router(proxy, deadline_seconds=0.15).complete(
            request(), routed, plan(routed), attempts
        )

    assert "deadline" in raised.value.message
    # `c` is never reached: the budget was spent inside `b`.
    assert proxy.called == ["a", "b"]


async def test_a_single_slow_target_cannot_outlive_the_deadline() -> None:
    slow = target("slow")
    proxy = ScriptedProxy()
    proxy.delays = {"slow": 5.0}

    routed = gateway(slow)
    with pytest.raises(UpstreamTimeout):
        await router(proxy, deadline_seconds=0.1).complete(
            request(), routed, plan(routed), Attempts()
        )


async def test_a_deadline_that_has_already_passed_is_still_a_timeout_not_a_crash() -> None:
    routed = gateway(target("a"))
    with pytest.raises(UpstreamTimeout):
        await router(ScriptedProxy(), deadline_seconds=-1.0).complete(
            request(), routed, plan(routed), Attempts()
        )


async def test_backoff_is_paid_between_attempts_and_only_between_them(
    pauses: list[float],
) -> None:
    """Jitter protects a provider from a synchronised second wave; it must not be paid by
    a request that never retried."""
    a, b = target("a"), target("b")
    routing = Router(ScriptedProxy({"a": status(503)}), backoff=lambda: 0.01)  # type: ignore[arg-type]

    routed = gateway(a, b, mode="failover")
    await routing.complete(request(), routed, plan(routed), Attempts())

    assert pauses == [0.01]


async def test_a_chain_that_never_retried_never_paused(pauses: list[float]) -> None:
    one = gateway(target("a"))
    routing = Router(ScriptedProxy(), backoff=lambda: 0.01)  # type: ignore[arg-type]

    await routing.complete(request(), one, plan(one), Attempts())

    assert pauses == []


async def test_the_pause_is_clamped_to_what_is_left_of_the_deadline(
    pauses: list[float],
) -> None:
    """A backoff longer than the whole budget must not be slept through and then
    followed by an attempt anyway."""
    a, b = target("a"), target("b")
    routed = gateway(a, b, mode="failover")
    routing = router(ScriptedProxy({"a": status(503)}), deadline_seconds=0.05, backoff=lambda: 30.0)

    await routing.complete(request(), routed, plan(routed), Attempts())

    assert pauses and pauses[0] <= 0.05


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


async def test_every_attempt_is_counted_by_model_and_outcome() -> None:
    from prometheus_client import CollectorRegistry

    from app.core.metrics import build_routing_metrics

    metrics = build_routing_metrics(CollectorRegistry())
    a, b = target("a"), target("b")
    proxy = ScriptedProxy({"a": status(503)})
    routed = gateway(a, b, mode="failover")

    await Router(proxy, backoff=lambda: 0.0, metrics=metrics).complete(  # type: ignore[arg-type]
        request(), routed, plan(routed), Attempts()
    )

    assert metrics.attempts.labels(mode="failover", model="a", outcome="failed")._value.get() == 1
    assert metrics.attempts.labels(mode="failover", model="b", outcome="served")._value.get() == 1
    assert metrics.failovers.labels(model="a", error_code="upstream_error")._value.get() == 1


def test_a_target_can_be_replaced_without_touching_its_weight() -> None:
    """`weights` is keyed by model id rather than by position, so reordering a chain
    cannot silently move a 70 onto the wrong model."""
    a, b = target("a"), target("b")
    weights = {a.id: 70, b.id: 30}
    reordered = gateway(b, a, mode="ab_split", weights=weights)

    picks = Counter(
        plan(reordered, end_user_key=f"user-{index}").targets[0].name for index in range(2000)
    )

    assert abs(picks["a"] / 20 - 70) <= 3


def test_a_plan_is_unaffected_by_a_copy_of_the_gateway() -> None:
    """`replace` is how the harness retargets a gateway; the weights have to survive it."""
    a, b = target("a"), target("b")
    routed = gateway(a, b, mode="ab_split", weights={a.id: 100, b.id: 0})

    assert plan(replace(routed), end_user_key="u").targets[0].name == "a"
