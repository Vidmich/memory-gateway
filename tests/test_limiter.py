"""One request's passage through its gateway's limits.

The store is in memory and the limiter is the real class, so what these check is the
orchestration: which phase spends what, what a refusal raises, what happens when the
counters cannot be reached, and whether the optimistic token estimate is corrected
afterwards.
"""

from __future__ import annotations

import uuid

import pytest

from app.api.proxy.errors import RateLimitUnavailable
from app.core.errors import RateLimited
from app.schemas.gateway_config import Quota
from app.services.limits import Ceilings
from tests.limits_support import BrokenLimitStore, build_limits, limits_config
from tests.support import make_gateway, make_target

PERSON = uuid.UUID("22222222-2222-2222-2222-222222222222")


def gateway(*, global_models: bool = False, **caps: object):  # type: ignore[no-untyped-def]
    per_end_user = caps.pop("per_end_user", None)
    return make_gateway(
        make_target("http://upstream.invalid/v1"),
        limits=limits_config(per_end_user=per_end_user, **caps),  # type: ignore[arg-type]
        global_models=global_models,
    )


# ---------------------------------------------------------------------------
# the two phases
# ---------------------------------------------------------------------------


async def test_a_gateway_with_no_limits_never_calls_the_store() -> None:
    """The default, and the reason this feature costs an unconfigured gateway nothing."""
    fixture = build_limits(store=BrokenLimitStore())
    limits = fixture.limiter.begin(gateway(), end_user_id=PERSON, holder="r1")

    await limits.requests()
    await limits.tokens(500)

    assert fixture.store.calls == []


async def test_the_limit_is_enforced_and_the_refusal_is_a_429() -> None:
    fixture = build_limits()
    # One gateway object, because the bucket is keyed by its id: two calls to `gateway()`
    # would be two endpoints with two budgets, and the test would pass for that reason.
    config = gateway(requests_per_minute=2)
    for index in range(2):
        await fixture.limiter.begin(config, end_user_id=PERSON, holder=f"r{index}").requests()

    limits = fixture.limiter.begin(config, end_user_id=PERSON, holder="r2")
    with pytest.raises(RateLimited) as failure:
        await limits.requests()

    assert failure.value.status_code == 429
    assert failure.value.openai_type == "rate_limit_error"
    assert int(failure.value.headers["retry-after"]) >= 1


async def test_one_end_user_being_throttled_does_not_touch_another() -> None:
    """Task 14's acceptance criterion, and the point of the second scope."""
    fixture = build_limits()
    config = gateway(per_end_user={"requests_per_minute": 1})
    other = uuid.uuid4()

    await fixture.limiter.begin(config, end_user_id=PERSON, holder="a").requests()
    with pytest.raises(RateLimited):
        await fixture.limiter.begin(config, end_user_id=PERSON, holder="b").requests()

    await fixture.limiter.begin(config, end_user_id=other, holder="c").requests()


async def test_token_limits_are_spent_by_the_estimate() -> None:
    fixture = build_limits()
    config = gateway(tokens_per_minute=1000)

    await fixture.limiter.begin(config, end_user_id=PERSON, holder="a").tokens(900)

    with pytest.raises(RateLimited) as failure:
        await fixture.limiter.begin(config, end_user_id=PERSON, holder="b").tokens(200)
    assert "tokens per minute" in failure.value.message


async def test_an_estimate_is_only_worth_computing_when_a_token_limit_applies() -> None:
    """The request path assembles the prompt a second time to answer ``tokens``. This is
    the flag that keeps every other gateway from paying for that."""
    fixture = build_limits()

    assert not fixture.limiter.begin(
        gateway(requests_per_minute=10), end_user_id=PERSON, holder="a"
    ).needs_estimate
    assert fixture.limiter.begin(
        gateway(tokens_per_minute=10), end_user_id=PERSON, holder="a"
    ).needs_estimate


async def test_a_per_end_user_token_limit_needs_no_estimate_for_an_anonymous_caller() -> None:
    fixture = build_limits()
    config = gateway(per_end_user={"tokens_per_minute": 10})

    assert not fixture.limiter.begin(config, end_user_id=None, holder="a").needs_estimate
    assert fixture.limiter.begin(config, end_user_id=PERSON, holder="a").needs_estimate


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


async def test_a_slot_is_taken_and_given_back() -> None:
    fixture = build_limits()
    config = gateway(concurrent_requests=1)

    held = fixture.limiter.begin(config, end_user_id=PERSON, holder="a")
    await held.tokens(0)
    with pytest.raises(RateLimited):
        await fixture.limiter.begin(config, end_user_id=PERSON, holder="b").tokens(0)

    await held.release()
    await fixture.limiter.begin(config, end_user_id=PERSON, holder="b").tokens(0)


async def test_releasing_twice_does_not_free_someone_elses_slot() -> None:
    """The route releases in a ``finally`` that can run after the observer already has."""
    fixture = build_limits()
    config = gateway(concurrent_requests=1)
    held = fixture.limiter.begin(config, end_user_id=PERSON, holder="a")
    await held.tokens(0)

    await held.release()
    other = fixture.limiter.begin(config, end_user_id=PERSON, holder="b")
    await other.tokens(0)
    await held.release()

    with pytest.raises(RateLimited):
        await fixture.limiter.begin(config, end_user_id=PERSON, holder="c").tokens(0)


async def test_a_rejected_request_takes_no_slot_to_release() -> None:
    """The atomicity that matters most on this path: a request refused on tokens must
    not be holding a concurrency slot that only a ``finally`` will notice."""
    fixture = build_limits()
    config = gateway(tokens_per_minute=10, concurrent_requests=5)

    with pytest.raises(RateLimited):
        await fixture.limiter.begin(config, end_user_id=PERSON, holder="a").tokens(1000)

    slots = fixture.store.slots
    assert all(not holders for holders in slots.values())


# ---------------------------------------------------------------------------
# settlement
# ---------------------------------------------------------------------------


async def test_an_underestimate_is_settled_upwards() -> None:
    fixture = build_limits()
    config = gateway(tokens_per_minute=1000)
    limits = fixture.limiter.begin(config, end_user_id=PERSON, holder="a")
    await limits.tokens(100)

    await limits.settle(prompt_tokens=100, completion_tokens=600)

    with pytest.raises(RateLimited):
        await fixture.limiter.begin(config, end_user_id=PERSON, holder="b").tokens(400)


async def test_an_overestimate_is_given_back() -> None:
    fixture = build_limits()
    config = gateway(tokens_per_minute=1000)
    limits = fixture.limiter.begin(config, end_user_id=PERSON, holder="a")
    await limits.tokens(900)

    await limits.settle(prompt_tokens=100, completion_tokens=50)

    await fixture.limiter.begin(config, end_user_id=PERSON, holder="b").tokens(800)


async def test_a_provider_that_reports_no_usage_leaves_the_estimate_standing() -> None:
    """Several do, for streams. An unknown cost counted as the estimate is closer than
    an unknown cost counted as free."""
    fixture = build_limits()
    config = gateway(tokens_per_minute=1000)
    limits = fixture.limiter.begin(config, end_user_id=PERSON, holder="a")
    await limits.tokens(900)

    await limits.settle(prompt_tokens=None, completion_tokens=None)

    with pytest.raises(RateLimited):
        await fixture.limiter.begin(config, end_user_id=PERSON, holder="b").tokens(200)


async def test_nothing_is_settled_when_no_token_limit_applied() -> None:
    fixture = build_limits(store=BrokenLimitStore())
    limits = fixture.limiter.begin(gateway(requests_per_minute=10), end_user_id=PERSON, holder="a")

    await limits.settle(prompt_tokens=10, completion_tokens=10)

    assert "settle" not in fixture.store.calls


# ---------------------------------------------------------------------------
# the outage
# ---------------------------------------------------------------------------


async def test_an_unreachable_store_serves_the_request_and_counts_it() -> None:
    """Task 14's acceptance criterion. Fail-open is a real trade — during the outage the
    shared upstream key is unprotected — so the counter behind it is not optional."""
    fixture = build_limits(store=BrokenLimitStore(), fail_open=True)
    limits = fixture.limiter.begin(gateway(requests_per_minute=1), end_user_id=PERSON, holder="a")

    await limits.requests()

    assert limits.degraded
    assert fixture.count("rate_limit_unavailable_total", policy="fail_open") == 1.0


async def test_failing_closed_refuses_with_a_503_rather_than_a_429() -> None:
    """The client did nothing wrong: nothing was counted and no budget was exceeded. A
    429 would tell them to slow down, which is not the fix."""
    fixture = build_limits(store=BrokenLimitStore(), fail_open=False)
    limits = fixture.limiter.begin(gateway(requests_per_minute=1), end_user_id=PERSON, holder="a")

    with pytest.raises(RateLimitUnavailable) as failure:
        await limits.requests()

    assert failure.value.status_code == 503
    assert fixture.count("rate_limit_unavailable_total", policy="fail_closed") == 1.0


async def test_an_unreachable_store_does_not_break_a_release() -> None:
    """The lease reclaims the slot. Raising here would turn a Redis blip into a 500 on a
    request that had already succeeded."""
    fixture = build_limits(store=BrokenLimitStore())
    limits = fixture.limiter.begin(gateway(concurrent_requests=1), end_user_id=PERSON, holder="a")
    await limits.tokens(0)

    await limits.release()


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


async def test_a_rejection_is_counted_by_limit_and_scope() -> None:
    fixture = build_limits()
    config = gateway(per_end_user={"requests_per_minute": 1})
    await fixture.limiter.begin(config, end_user_id=PERSON, holder="a").requests()

    with pytest.raises(RateLimited):
        await fixture.limiter.begin(config, end_user_id=PERSON, holder="b").requests()

    assert (
        fixture.count("rate_limit_rejections_total", limit="requests_per_minute", scope="end_user")
        == 1.0
    )


async def test_running_hot_is_counted_before_anybody_is_refused() -> None:
    """The signal that arrives in time to do something about it."""
    fixture = build_limits()
    config = gateway(requests_per_minute=5)

    for index in range(4):
        await fixture.limiter.begin(config, end_user_id=PERSON, holder=f"h{index}").requests()

    assert (
        fixture.count("rate_limit_near_limit_total", limit="requests_per_minute", scope="gateway")
        == 1.0
    )


async def test_every_check_is_timed() -> None:
    """How task 14's "< 2 ms p95" is answerable in production rather than in a comment."""
    fixture = build_limits()
    await fixture.limiter.begin(
        gateway(requests_per_minute=5), end_user_id=PERSON, holder="a"
    ).requests()

    assert fixture.count("rate_limit_check_duration_seconds_count") == 1.0


# ---------------------------------------------------------------------------
# the ceiling, through the limiter
# ---------------------------------------------------------------------------


async def test_the_platform_ceiling_binds_a_gateway_on_a_global_model() -> None:
    """Not through the control plane — this is enforcement, so it holds for a limit
    saved before the ceiling existed and for a gateway repointed at a global model
    since."""
    fixture = build_limits(ceilings=Ceilings(requests_per_minute=2))
    config = gateway(requests_per_minute=1000, global_models=True)

    for index in range(2):
        await fixture.limiter.begin(config, end_user_id=PERSON, holder=f"h{index}").requests()

    with pytest.raises(RateLimited):
        await fixture.limiter.begin(config, end_user_id=PERSON, holder="h2").requests()


async def test_an_unlimited_gateway_on_a_global_model_is_still_bounded() -> None:
    fixture = build_limits(ceilings=Ceilings(requests_per_minute=1))
    config = gateway(global_models=True)

    await fixture.limiter.begin(config, end_user_id=PERSON, holder="a").requests()

    with pytest.raises(RateLimited):
        await fixture.limiter.begin(config, end_user_id=PERSON, holder="b").requests()


async def test_the_ceiling_leaves_an_organizations_own_models_alone() -> None:
    fixture = build_limits(ceilings=Ceilings(requests_per_minute=1))
    config = gateway(requests_per_minute=10, global_models=False)

    for index in range(10):
        await fixture.limiter.begin(config, end_user_id=PERSON, holder=f"h{index}").requests()


# ---------------------------------------------------------------------------
# headers
# ---------------------------------------------------------------------------


async def test_headers_report_what_is_left_after_this_request() -> None:
    fixture = build_limits()
    limits = fixture.limiter.begin(gateway(requests_per_minute=10), end_user_id=PERSON, holder="a")

    await limits.requests()

    assert limits.headers()["X-RateLimit-Limit"] == "10"
    assert limits.headers()["X-RateLimit-Remaining"] == "9"
    assert int(limits.headers()["X-RateLimit-Reset"]) > 0


async def test_headers_cover_both_phases() -> None:
    """A gateway limited on tokens and on requests reports whichever is tighter, and the
    late phase's reading has to be in the running for that."""
    fixture = build_limits()
    limits = fixture.limiter.begin(
        gateway(requests_per_minute=1000, tokens_per_minute=100),
        end_user_id=PERSON,
        holder="a",
    )

    await limits.requests()
    await limits.tokens(95)

    assert limits.headers()["X-RateLimit-Limit"] == "100"
    assert limits.headers()["X-RateLimit-Remaining"] == "5"


async def test_a_degraded_check_reports_no_headers_rather_than_wrong_ones() -> None:
    fixture = build_limits(store=BrokenLimitStore())
    limits = fixture.limiter.begin(gateway(requests_per_minute=10), end_user_id=PERSON, holder="a")

    await limits.requests()

    assert limits.headers() == {}


def test_a_quota_knows_when_it_is_empty() -> None:
    assert Quota().unlimited
    assert not Quota(requests_per_day=1).unlimited
