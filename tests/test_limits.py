"""SPEC §11's rules, as arithmetic.

Nothing here touches Redis, a gateway or a request. What is being checked is the part of
rate limiting that is a *decision*: which caps apply to which request, in what order they
are weighed, which one a 429 names, what the ``X-RateLimit-*`` headers claim, and what the
platform ceiling does to a number an organization typed.
"""

from __future__ import annotations

import uuid

import pytest

from app.schemas.gateway_config import LimitsConfig, Quota
from app.services.limits import (
    EARLY_LIMITS,
    LATE_LIMITS,
    NEAR_LIMIT,
    Ceilings,
    Reading,
    Rule,
    bucket_key,
    effective,
    headers,
    message,
    plan,
    retry_after,
    tightest,
)

GATEWAY = uuid.UUID("11111111-1111-1111-1111-111111111111")
PERSON = uuid.UUID("22222222-2222-2222-2222-222222222222")


def reading(
    limit: str = "requests_per_minute",
    *,
    value: int = 10,
    remaining: int = 5,
    reset: int = 30,
    window: int = 60,
    scope: str = "gateway",
    allowed: bool = True,
) -> Reading:
    return Reading(
        rule=Rule(
            scope=scope,  # type: ignore[arg-type]
            limit=limit,
            value=value,
            key="rl:test",
            window_seconds=window,
        ),
        allowed=allowed,
        remaining=remaining,
        reset_seconds=reset,
    )


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


def test_an_unlimited_gateway_produces_no_rules() -> None:
    """Which is what keeps rate limiting off the latency budget of every gateway that
    has not configured it: no rules means no Redis call at all."""
    result = plan(
        effective(LimitsConfig()),
        gateway_id=GATEWAY,
        end_user_id=PERSON,
        names=EARLY_LIMITS + LATE_LIMITS,
    )

    assert result == ()


def test_both_scopes_are_planned_when_both_are_configured() -> None:
    limits = LimitsConfig(requests_per_minute=100, per_end_user=Quota(requests_per_minute=10))

    result = plan(effective(limits), gateway_id=GATEWAY, end_user_id=PERSON, names=EARLY_LIMITS)

    assert [(item.scope, item.value) for item in result] == [("gateway", 100), ("end_user", 10)]


def test_the_gateways_own_cap_is_weighed_before_any_callers() -> None:
    """So the 429 for an endpoint that is over budget as a whole says so, rather than
    blaming whichever caller happened to arrive at the wrong moment."""
    limits = LimitsConfig(requests_per_minute=1, per_end_user=Quota(requests_per_minute=1))

    result = plan(effective(limits), gateway_id=GATEWAY, end_user_id=PERSON, names=EARLY_LIMITS)

    assert result[0].scope == "gateway"


def test_a_per_end_user_cap_is_skipped_when_nobody_is_identified() -> None:
    """SPEC §6.2 allows an unidentified caller. Bucketing them together would throttle
    every anonymous request as though they were one very busy person."""
    limits = LimitsConfig(per_end_user=Quota(requests_per_minute=10))

    result = plan(effective(limits), gateway_id=GATEWAY, end_user_id=None, names=EARLY_LIMITS)

    assert result == ()


def test_requests_are_weighed_before_tokens_and_concurrency() -> None:
    """Cheapest first: counting is two integers, a token estimate is an assembled
    prompt, and a slot is the only one that has to be handed back."""
    assert EARLY_LIMITS == ("requests_per_minute", "requests_per_day")
    assert LATE_LIMITS == ("tokens_per_minute", "concurrent_requests")


def test_a_key_names_the_limit_not_the_window() -> None:
    """Two limits share the minute window; a key naming only the window would have them
    spending each other's budget."""
    minute = bucket_key("requests_per_minute", gateway_id=GATEWAY)
    tokens = bucket_key("tokens_per_minute", gateway_id=GATEWAY)

    assert minute != tokens


def test_a_per_end_user_key_carries_the_row_id_not_the_header() -> None:
    """The external id can be an email address, and a key space full of those is a
    mailing list in a cache nobody thinks of as a data store."""
    key = bucket_key("requests_per_minute", gateway_id=GATEWAY, end_user_id=PERSON)

    assert str(PERSON) in key
    assert key != bucket_key("requests_per_minute", gateway_id=GATEWAY)


# ---------------------------------------------------------------------------
# the ceiling
# ---------------------------------------------------------------------------


def test_the_ceiling_does_nothing_to_a_gateway_on_its_own_models() -> None:
    limits = LimitsConfig(requests_per_minute=5000)

    result = effective(limits, ceilings=Ceilings(requests_per_minute=600), global_models=False)

    assert result.gateway.requests_per_minute == 5000
    assert result.capped == ()


def test_the_ceiling_lowers_a_limit_on_a_gateway_using_a_global_model() -> None:
    limits = LimitsConfig(requests_per_minute=5000)

    result = effective(limits, ceilings=Ceilings(requests_per_minute=600), global_models=True)

    assert result.gateway.requests_per_minute == 600
    assert result.capped == ("requests_per_minute",)


def test_the_ceiling_applies_to_an_unlimited_gateway_too() -> None:
    """The most exposed configuration there is, and the one the ceiling exists for: an
    org that has set nothing is spending the operator's key without a bound."""
    result = effective(
        LimitsConfig(), ceilings=Ceilings(requests_per_minute=600), global_models=True
    )

    assert result.gateway.requests_per_minute == 600
    assert result.capped == ("requests_per_minute",)


def test_a_stricter_gateway_keeps_its_own_number() -> None:
    """A ceiling that also *raised* limits would loosen every careful configuration on
    the platform the day the operator set one."""
    limits = LimitsConfig(requests_per_minute=10)

    result = effective(limits, ceilings=Ceilings(requests_per_minute=600), global_models=True)

    assert result.gateway.requests_per_minute == 10
    assert result.capped == ()


def test_the_ceiling_leaves_the_per_end_user_block_alone() -> None:
    """It protects the operator's credential, which the gateway as a whole spends. A
    per-person cap above the gateway's is a number that never binds, not a loophole."""
    limits = LimitsConfig(per_end_user=Quota(requests_per_minute=5000))

    result = effective(limits, ceilings=Ceilings(requests_per_minute=600), global_models=True)

    assert result.per_end_user.requests_per_minute == 5000


def test_ceilings_are_read_off_the_settings_by_name() -> None:
    class FakeSettings:
        global_model_requests_per_minute = 60
        global_model_tokens_per_minute = None
        global_model_requests_per_day = 1000
        global_model_concurrent_requests = 4

    result = Ceilings.of(FakeSettings())

    assert result.requests_per_minute == 60
    assert result.requests_per_day == 1000
    assert result.concurrent_requests == 4
    assert result.tokens_per_minute is None


def test_no_ceilings_at_all_is_the_default() -> None:
    assert Ceilings().empty


# ---------------------------------------------------------------------------
# headers and messages
# ---------------------------------------------------------------------------


def test_the_headers_describe_the_tightest_limit_by_fraction() -> None:
    """900 of 1000 tokens left is not tighter than 3 of 10 requests, and a client that
    slowed down for the first would be reacting to the wrong number."""
    result = headers(
        (
            reading("tokens_per_minute", value=1000, remaining=900),
            reading("requests_per_minute", value=10, remaining=3),
        )
    )

    assert result["X-RateLimit-Limit"] == "10"
    assert result["X-RateLimit-Remaining"] == "3"


def test_concurrency_is_never_reported_in_the_headers() -> None:
    """``X-RateLimit-Reset`` for a slot would be a lie: it frees when some other request
    finishes, not at a time anybody can name."""
    result = headers((reading("concurrent_requests", value=4, remaining=0, window=0),))

    assert result == {}


def test_a_gateway_with_no_limits_sends_no_headers() -> None:
    """``X-RateLimit-Remaining: 0`` on an unlimited endpoint would have a well-behaved
    client back off forever."""
    assert headers(()) == {}


def test_the_shorter_window_wins_a_tie() -> None:
    minute = reading("requests_per_minute", value=10, remaining=5, window=60)
    day = reading("requests_per_day", value=1000, remaining=500, window=86_400)

    assert tightest((day, minute)) is minute


def test_the_message_names_the_limit_and_whose_it_is() -> None:
    """ "10 requests per minute" is a different problem to solve depending on whether it
    is the endpoint's budget or one caller's."""
    text = message(reading("requests_per_minute", value=10, remaining=0, scope="end_user"))

    assert "10 requests per minute" in text
    assert "this end user" in text


def test_retry_after_is_the_rest_of_the_window() -> None:
    assert retry_after(reading(reset=17)) == 17


def test_retry_after_is_never_zero() -> None:
    """A 429 telling a client to wait no time at all is a 429 it will ignore."""
    assert retry_after(reading(reset=0)) >= 1


def test_a_concurrency_rejection_says_one_second() -> None:
    """A slot is usually free again in two hundred milliseconds. Telling a client to
    wait a minute turns a brief queue into an outage."""
    assert retry_after(reading("concurrent_requests", value=4, reset=0, window=0)) == 1


# ---------------------------------------------------------------------------
# readings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [(10, 0.0), (5, 0.5), (0, 1.0)],
)
def test_utilization_is_what_is_spent(remaining: int, expected: float) -> None:
    assert reading(value=10, remaining=remaining).utilization == expected


def test_near_limit_starts_at_the_warning_threshold() -> None:
    assert reading(value=10, remaining=2).near_limit
    assert not reading(value=10, remaining=3).near_limit
    assert NEAR_LIMIT == 0.8
