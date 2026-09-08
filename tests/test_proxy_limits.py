"""SPEC §11 on the request path.

The limiter is unit-tested next door; what these check is everything that only exists once
a real request is going through the gateway — the shape of the 429, the headers on every
response, the row a rejection leaves in the log, when the concurrency slot is given back,
and the one thing a client-side token count could never produce: an estimate that includes
the memory the gateway injected.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.core import background
from app.services.limits import CONCURRENCY_LEASE_SECONDS, Rule, bucket_key
from tests.conftest import ProxyHarness, eventually
from tests.support import Behaviour, chunk, completion


def payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"model": "demo", "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return body


async def post(proxy: ProxyHarness, *, user: str | None = None, **overrides: Any) -> Any:
    headers = proxy.headers()
    if user is not None:
        headers["X-Gateway-User"] = user
    return await proxy.client.post(proxy.url(), json=payload(**overrides), headers=headers)


# ---------------------------------------------------------------------------
# refusing
# ---------------------------------------------------------------------------


async def test_a_gateway_limited_to_n_admits_exactly_n(proxy: ProxyHarness) -> None:
    """The demo: set 2/minute, fire 4, watch two succeed and two be refused."""
    proxy.upstream.behaviour = Behaviour(body=completion())
    proxy.limit(requests_per_minute=2)

    statuses = [(await post(proxy)).status_code for _ in range(4)]

    assert statuses == [200, 200, 429, 429]


async def test_the_refusal_is_the_openai_error_shape(proxy: ProxyHarness) -> None:
    """So an SDK raises ``RateLimitError`` and backs off, instead of an opaque
    ``APIStatusError`` nobody's retry logic recognises."""
    proxy.upstream.behaviour = Behaviour(body=completion())
    proxy.limit(requests_per_minute=1)
    await post(proxy)

    response = await post(proxy)

    assert response.status_code == 429
    error = response.json()["error"]
    assert error["type"] == "rate_limit_error"
    assert set(error) == {"message", "type", "param", "code"}
    assert int(response.headers["retry-after"]) >= 1


async def test_the_message_names_the_limit_and_its_scope(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(body=completion())
    proxy.limit(per_end_user={"requests_per_minute": 1})
    await post(proxy, user="alice")

    response = await post(proxy, user="alice")

    assert "1 requests per minute" in response.json()["error"]["message"]
    assert "this end user" in response.json()["error"]["message"]


async def test_one_end_user_is_throttled_without_touching_another(
    proxy: ProxyHarness,
) -> None:
    proxy.upstream.behaviour = Behaviour(body=completion())
    proxy.limit(per_end_user={"requests_per_minute": 1})

    assert (await post(proxy, user="alice")).status_code == 200
    assert (await post(proxy, user="alice")).status_code == 429
    assert (await post(proxy, user="bob")).status_code == 200


async def test_a_throttled_request_never_reaches_the_provider(proxy: ProxyHarness) -> None:
    """Which is the whole point: the shared upstream key is what is being protected."""
    proxy.upstream.behaviour = Behaviour(body=completion())
    proxy.limit(requests_per_minute=1)
    await post(proxy)

    await post(proxy)

    assert len(proxy.upstream.requests) == 1


# ---------------------------------------------------------------------------
# headers
# ---------------------------------------------------------------------------


async def test_the_budget_is_on_a_successful_response(proxy: ProxyHarness) -> None:
    """On *every* response, not only refusals — a client that learns its limit by
    exceeding it cannot pace itself, which is what these headers are for."""
    proxy.upstream.behaviour = Behaviour(body=completion())
    proxy.limit(requests_per_minute=10)

    response = await post(proxy)

    assert response.headers["x-ratelimit-limit"] == "10"
    assert response.headers["x-ratelimit-remaining"] == "9"
    assert int(response.headers["x-ratelimit-reset"]) > 0


async def test_the_budget_is_on_the_refusal_too(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(body=completion())
    proxy.limit(requests_per_minute=1)
    await post(proxy)

    response = await post(proxy)

    assert response.headers["x-ratelimit-remaining"] == "0"
    assert response.headers["x-ratelimit-limit"] == "1"


async def test_the_budget_is_on_an_upstream_failure_too(proxy: ProxyHarness) -> None:
    """A 502 is exactly when a client is deciding whether to retry, and whether it has
    the budget to is part of that decision."""
    proxy.upstream.behaviour = Behaviour(status=500, body={"error": {"message": "boom"}})
    proxy.limit(requests_per_minute=10)

    response = await post(proxy)

    assert response.status_code == 500
    assert response.headers["x-ratelimit-limit"] == "10"


async def test_an_unlimited_gateway_sends_no_budget_headers(proxy: ProxyHarness) -> None:
    """``X-RateLimit-Remaining: 0`` on an endpoint with no limits would have a
    well-behaved client back off forever."""
    proxy.upstream.behaviour = Behaviour(body=completion())

    response = await post(proxy)

    assert "x-ratelimit-limit" not in response.headers


async def test_the_headers_are_on_a_stream_before_the_first_frame(
    proxy: ProxyHarness,
) -> None:
    proxy.upstream.behaviour = Behaviour(chunks=[chunk("hi")])
    proxy.limit(requests_per_minute=10)

    response = await post(proxy, stream=True)

    assert response.headers["x-ratelimit-remaining"] == "9"


# ---------------------------------------------------------------------------
# what a rejection leaves behind
# ---------------------------------------------------------------------------


async def test_a_rejection_is_logged_so_throttling_is_visible(proxy: ProxyHarness) -> None:
    """SPEC §11 wants an org to see when it is being throttled rather than guess. The
    row is what puts a rate-limit series on the error chart."""
    proxy.upstream.behaviour = Behaviour(body=completion())
    proxy.limit(requests_per_minute=1)
    await post(proxy)

    await post(proxy)
    await proxy.logs.flush()

    refused = [row for row in proxy.logs.rows if row.status_code == 429]
    assert len(refused) == 1
    assert refused[0].error_code == "rate_limited"


async def test_a_rejection_stores_metadata_and_no_transcript(proxy: ProxyHarness) -> None:
    """Nothing was done with the body and no model saw it. Storing end-user text for a
    request that never happened is cost and exposure with no reader."""
    proxy.upstream.behaviour = Behaviour(body=completion())
    proxy.limit(requests_per_minute=1)
    await post(proxy)

    await post(proxy)
    await proxy.logs.flush()

    refused = next(row for row in proxy.logs.rows if row.status_code == 429)
    assert proxy.logs.transcript(refused.id) is None
    assert refused.bodies_omitted == "rate_limited"


async def test_a_rejection_is_attributed_to_the_caller(proxy: ProxyHarness) -> None:
    """So "top throttled end users" has something to group by."""
    proxy.upstream.behaviour = Behaviour(body=completion())
    proxy.limit(per_end_user={"requests_per_minute": 1})
    await post(proxy, user="alice")

    await post(proxy, user="alice")
    await proxy.logs.flush()

    refused = next(row for row in proxy.logs.rows if row.status_code == 429)
    assert refused.end_user_id is not None


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------


async def test_the_token_estimate_includes_injected_memory(proxy: ProxyHarness) -> None:
    """Task 14's acceptance criterion, and the one a client-side count cannot reproduce:
    the tokens being paid for are the ones the *gateway* added."""
    proxy.upstream.behaviour = Behaviour(body=completion())
    organization = proxy.gateway.organization_id
    await proxy.memory.learn(
        organization,
        "alice",
        # Long enough that its presence in the prompt is the difference between fitting
        # under the cap and not.
        " ".join(["alice prefers extremely detailed answers about distributed systems"] * 20),
    )
    proxy.limit(tokens_per_minute=60)

    response = await post(proxy, user="alice")

    assert response.status_code == 429
    assert "tokens per minute" in response.json()["error"]["message"]


async def test_the_same_request_without_memory_fits_under_the_same_cap(
    proxy: ProxyHarness,
) -> None:
    """The other half of the previous test: without the injected block the prompt is a
    handful of tokens, so the cap is not what is doing the work in either case."""
    proxy.upstream.behaviour = Behaviour(body=completion())
    proxy.limit(tokens_per_minute=60)

    assert (await post(proxy, user="alice")).status_code == 200


async def test_usage_settles_the_estimate_after_the_response(proxy: ProxyHarness) -> None:
    """The estimate for "hi" is a few tokens; the provider reports five. The next
    request is weighed against what actually happened, not against the guess."""
    proxy.upstream.behaviour = Behaviour(body=completion())
    proxy.limit(tokens_per_minute=1000)

    await post(proxy)
    readings = await proxy.limits.store.peek([_token_rule(proxy)])

    # `completion()` reports 3 prompt + 2 completion tokens, and SPEC §11 counts both.
    assert readings[0].used == 5


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


async def test_the_slot_is_returned_after_a_normal_completion(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(body=completion())
    proxy.limit(concurrent_requests=1)

    for _ in range(3):
        assert (await post(proxy)).status_code == 200

    assert _slots_held(proxy) == 0


async def test_the_slot_is_returned_after_an_upstream_error(proxy: ProxyHarness) -> None:
    """A leaked counter permanently throttles a gateway, and an erroring provider is
    exactly when that would happen most."""
    proxy.upstream.behaviour = Behaviour(status=500, body={"error": {"message": "boom"}})
    proxy.limit(concurrent_requests=1)

    for _ in range(3):
        await post(proxy)

    assert _slots_held(proxy) == 0


async def test_the_slot_is_returned_after_a_stream_finishes(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(chunks=[chunk("a"), chunk("b")])
    proxy.limit(concurrent_requests=1)

    response = await post(proxy, stream=True)
    await response.aread()

    await eventually(lambda: _slots_held(proxy) == 0)


async def test_the_slot_is_returned_when_the_client_hangs_up(
    live_proxy: ProxyHarness,
) -> None:
    """Over a real socket, because a client disconnect does not exist in an in-process
    transport — and this is the case a ``finally`` in the route would not cover, since
    the route returned long before the client walked away."""
    live_proxy.upstream.behaviour = Behaviour(
        chunks=[chunk(str(index)) for index in range(200)], chunk_delay=0.05
    )
    live_proxy.limit(concurrent_requests=1)

    async with live_proxy.client.stream(
        "POST",
        live_proxy.url(),
        json=payload(stream=True),
        headers=live_proxy.headers(),
    ) as response:
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                break  # read one frame, then walk away

    await eventually(lambda: _slots_held(live_proxy) == 0)


async def test_a_refused_request_holds_no_slot(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(body=completion())
    proxy.limit(requests_per_minute=1, concurrent_requests=5)
    await post(proxy)

    await post(proxy)

    assert _slots_held(proxy) == 0


def _slots_held(proxy: ProxyHarness) -> int:
    return sum(len(holders) for holders in proxy.limits.store.slots.values())


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------


async def test_a_throttled_request_pays_for_no_retrieval(proxy: ProxyHarness) -> None:
    """ "Evaluate cheapest-first" as an observable fact rather than an ordering in a
    tuple: a request refused on the request counter never retrieves anything."""
    proxy.upstream.behaviour = Behaviour(body=completion())
    organization = proxy.gateway.organization_id
    await proxy.memory.learn(organization, "alice", "alice writes Rust")
    proxy.limit(requests_per_minute=1)
    await post(proxy, user="alice")

    response = await post(proxy, user="alice")

    assert response.status_code == 429
    # Retrieval sets this header whenever it ran at all, zero results included.
    assert "x-gateway-memory-facts" not in response.headers


# ---------------------------------------------------------------------------
# the outage
# ---------------------------------------------------------------------------


async def test_with_the_counters_down_requests_still_succeed(
    upstream: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 14's acceptance criterion. Fail-open is the default because making rate
    limiting a hard dependency turns a Redis blip into a full outage."""
    from httpx import ASGITransport, AsyncClient

    from tests.conftest import build_harness_parts, build_memory, build_proxy_app
    from tests.limits_support import BrokenLimitStore, build_limits
    from tests.monitoring_support import build_logs

    resolver, authenticator, token = build_harness_parts(upstream)
    limits = build_limits(store=BrokenLimitStore())
    application = build_proxy_app(resolver, authenticator, build_logs(), build_memory(), limits)
    upstream.behaviour = Behaviour(body=completion())

    import dataclasses

    from tests.limits_support import limits_config

    resolver.gateway = dataclasses.replace(
        resolver.gateway, limits=limits_config(requests_per_minute=1)
    )

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            statuses = [
                (
                    await client.post(
                        "/g/demo/v1/chat/completions",
                        json=payload(),
                        headers={"Authorization": f"Bearer {token}"},
                    )
                ).status_code
                for _ in range(3)
            ]

    assert statuses == [200, 200, 200]
    assert limits.count("rate_limit_unavailable_total", policy="fail_open") >= 1.0


async def test_the_lease_is_long_enough_for_the_longest_request() -> None:
    """A slot reclaimed while its holder is still generating would over-admit; one never
    reclaimed would throttle the gateway forever. The lease is set well past the routing
    deadline for the first reason and finite for the second."""
    from app.core.config import get_settings

    assert get_settings().routing_deadline_seconds < CONCURRENCY_LEASE_SECONDS


def _token_rule(proxy: ProxyHarness) -> Rule:
    """The gateway's token bucket, built by hand so a test can read it without going
    through the object under test."""
    return Rule(
        scope="gateway",
        limit="tokens_per_minute",
        value=1000,
        key=bucket_key("tokens_per_minute", gateway_id=proxy.gateway.id),
        window_seconds=60,
    )


@pytest.fixture(autouse=True)
async def _drain_background() -> AsyncIterator[None]:
    """A stream settles its limits from a spawned task. Draining keeps one test's
    settlement from landing in the middle of the next one's assertions."""
    yield
    await background.drain(timeout_seconds=2.0)
    await asyncio.sleep(0)
