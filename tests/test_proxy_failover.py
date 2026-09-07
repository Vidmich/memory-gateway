"""Failover and A/B through the whole stack: two real providers on two real sockets.

``tests/test_routing.py`` proves the decisions against a scripted proxy. This file proves
they survive the parts a fake cannot have: HTTP status codes coming back over a socket, an
SSE body whose status line is committed the moment the first frame is written, and the
request log row somebody will later open the drawer on.

Two providers rather than one with a counter, because the thing being demonstrated is
that traffic *moved* — the secondary's own request list is the evidence, and a counter on
a single mock would prove only that the gateway called something twice.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.adapters.base import UpstreamTarget
from app.api.proxy.deps import get_router
from app.db.models import RequestLog
from app.services.proxy import ProxyService
from app.services.routing import Router
from tests.conftest import build_proxy_app
from tests.monitoring_support import LogFixture, build_logs
from tests.support import (
    Behaviour,
    FakeAuthenticator,
    FakeResolver,
    MockUpstream,
    chunk,
    completion,
    make_gateway,
    make_target,
    serve,
)

pytestmark = pytest.mark.anyio


@dataclass
class Chain:
    """A gateway with two upstreams behind it, and the log that watched."""

    client: AsyncClient
    primary: MockUpstream
    secondary: MockUpstream
    resolver: FakeResolver
    token: str
    logs: LogFixture

    def route(self, mode: str, *, weights: dict[uuid.UUID, int] | None = None) -> None:
        self.resolver.gateway = replace(
            self.resolver.gateway, routing_mode=mode, weights=weights or {}
        )

    def retarget(self, index: int, **overrides: Any) -> None:
        targets = list(self.resolver.gateway.targets)
        targets[index] = replace(targets[index], **overrides)
        self.resolver.gateway = replace(self.resolver.gateway, targets=tuple(targets))

    @property
    def targets(self) -> Sequence[UpstreamTarget]:
        return self.resolver.gateway.targets

    async def send(self, **body: Any) -> Any:
        payload = {"model": "demo", "messages": [{"role": "user", "content": "hi"}]}
        payload.update(body)
        return await self.client.post(
            "/g/demo/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"},
        )

    async def row(self) -> RequestLog:
        await self.logs.flush()
        return self.logs.rows[-1]


@pytest.fixture
async def chain(upstream: MockUpstream) -> AsyncIterator[Chain]:
    secondary = MockUpstream()
    async with serve(secondary) as base_url:
        secondary.base_url = base_url

        first = make_target(f"{upstream.base_url}/v1", name="primary", timeout_seconds=2)
        second = make_target(f"{secondary.base_url}/v1", name="secondary", timeout_seconds=2)
        gateway = make_gateway(first, targets=(first, second), routing_mode="failover")

        resolver = FakeResolver(gateway=gateway)
        authenticator = FakeAuthenticator()
        token = authenticator.issue(gateway.id)
        logs = build_logs()
        application = build_proxy_app(resolver, authenticator, logs)
        _no_backoff(application)

        async with application.router.lifespan_context(application):
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                yield Chain(
                    client=client,
                    primary=upstream,
                    secondary=secondary,
                    resolver=resolver,
                    token=token,
                    logs=logs,
                )


def _no_backoff(application: FastAPI) -> None:
    """The real router, minus the jitter.

    Fifty to a hundred and fifty milliseconds per retry is protection for a provider
    under load; paying it in a test suite buys nothing and makes the slowest tests here
    the ones that retry most.
    """

    def build() -> Router:
        return Router(ProxyService(application.state.clients.http), backoff=lambda: 0.0)

    application.dependency_overrides[get_router] = build


# ---------------------------------------------------------------------------
# failover
# ---------------------------------------------------------------------------


async def test_a_broken_primary_still_answers(chain: Chain) -> None:
    """The acceptance criterion: a deliberately broken first target, a working second,
    and a client that never finds out."""
    chain.primary.behaviour = Behaviour(status=503, body={"error": {"message": "down"}})
    chain.secondary.behaviour = Behaviour(body=completion("from the spare"))

    response = await chain.send()

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "from the spare"
    assert response.headers["X-Gateway-Model"] == "secondary"
    assert len(chain.primary.requests) == 1
    assert len(chain.secondary.requests) == 1


async def test_both_attempts_appear_in_the_log(chain: Chain) -> None:
    chain.primary.behaviour = Behaviour(status=503, body={"error": {"message": "down"}})
    chain.secondary.behaviour = Behaviour(body=completion("ok"))

    await chain.send()
    row = await chain.row()

    assert row.status_code == 200
    assert row.model_name == "secondary"
    assert [attempt["model_name"] for attempt in row.failover_attempts] == [
        "primary",
        "secondary",
    ]
    assert [attempt["status"] for attempt in row.failover_attempts] == [503, 200]
    assert row.failover_attempts[0]["retryable"] is True


async def test_a_four_hundred_from_the_primary_is_returned_to_the_client(chain: Chain) -> None:
    """SPEC §8.2: the next target would reject it identically. Trying anyway turns one
    bad request into two and doubles the caller's wait for their own mistake."""
    chain.primary.behaviour = Behaviour(
        status=400, body={"error": {"message": "bad temperature", "code": "invalid_value"}}
    )

    response = await chain.send()

    assert response.status_code == 400
    assert chain.secondary.requests == []


async def test_the_client_sees_the_last_error_when_the_whole_chain_fails(chain: Chain) -> None:
    chain.primary.behaviour = Behaviour(status=503, body={"error": {"message": "down"}})
    chain.secondary.behaviour = Behaviour(status=429, body={"error": {"message": "slow down"}})

    response = await chain.send()

    assert response.status_code == 429
    row = await chain.row()
    assert [attempt["status"] for attempt in row.failover_attempts] == [503, 429]
    # Both were worth retrying: the chain was too short, not the error final.
    assert all(attempt["retryable"] for attempt in row.failover_attempts)


async def test_a_healthy_primary_never_reaches_the_secondary(chain: Chain) -> None:
    chain.primary.behaviour = Behaviour(body=completion("first time"))

    response = await chain.send()

    assert response.status_code == 200
    assert chain.secondary.requests == []


async def test_one_clean_attempt_leaves_no_timeline(chain: Chain) -> None:
    """An empty array means "one target answered", which the row's own columns already
    describe. Writing a one-element array on every request would cost storage on every
    row to restate them."""
    chain.primary.behaviour = Behaviour(body=completion("fine"))

    await chain.send()

    assert (await chain.row()).failover_attempts == []


async def test_the_stored_prompt_is_the_one_the_answering_target_received(
    chain: Chain,
) -> None:
    """Two targets can carry different system contexts, so the transcript has to follow
    the chain rather than be captured once before the first call."""
    chain.retarget(0, system_context="I am the primary.")
    chain.retarget(1, system_context="I am the spare.")
    chain.primary.behaviour = Behaviour(status=503, body={"error": {"message": "down"}})
    chain.secondary.behaviour = Behaviour(body=completion("ok"))

    await chain.send()
    row = await chain.row()
    transcript = chain.logs.transcript(row.id)

    assert transcript is not None
    assert transcript.assembled_prompt is not None
    assert transcript.assembled_prompt[0]["content"] == "I am the spare."


async def test_a_dead_socket_is_retried_like_a_five_hundred(chain: Chain) -> None:
    """A refused connection reaches the router as the 502 the proxy turned it into, which
    is what keeps the classification table complete without a transport branch."""
    chain.retarget(0, base_url="http://127.0.0.1:1/v1")
    chain.secondary.behaviour = Behaviour(body=completion("rescued"))

    response = await chain.send()

    assert response.status_code == 200
    assert response.headers["X-Gateway-Model"] == "secondary"


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


async def test_a_stream_fails_over_before_the_first_byte(chain: Chain) -> None:
    """``open_stream`` validates the status before yielding anything, so a provider 503
    on a streamed request is still an ordinary HTTP failure the next target absorbs."""
    chain.primary.behaviour = Behaviour(status=503, body={"error": {"message": "down"}})
    chain.secondary.behaviour = Behaviour(chunks=[chunk("streamed"), chunk(" answer")])

    response = await chain.send(stream=True)

    assert response.status_code == 200
    assert response.headers["X-Gateway-Model"] == "secondary"
    assert "streamed" in response.text
    assert response.text.rstrip().endswith("data: [DONE]")


async def test_a_stream_that_dies_after_the_first_frame_cannot_fail_over(
    chain: Chain,
) -> None:
    """SPEC §8.2. The 200 went out with the first frame, so the only honest ending is an
    error event — and the flag on the row is what says failover was never an option."""
    chain.retarget(0, timeout_seconds=1)
    chain.primary.behaviour = Behaviour(chunks=[chunk("half an ans")], stall_seconds=3)

    response = await chain.send(stream=True)

    assert response.status_code == 200
    assert "half an ans" in response.text
    error = json.loads(_frames(response.text)[-1])["error"]
    assert error["code"] == "upstream_timeout"
    # Never tried: the response had already begun.
    assert chain.secondary.requests == []

    row = await chain.row()
    assert row.status_code == 200
    assert row.error_code == "stream_failed"
    assert row.failed_after_stream_start is True


async def test_a_stream_that_completes_sets_no_failure_flag(chain: Chain) -> None:
    chain.primary.behaviour = Behaviour(chunks=[chunk("all good")])

    await chain.send(stream=True)
    row = await chain.row()

    assert row.failed_after_stream_start is False
    assert row.error_code is None


async def test_a_recovered_stream_records_both_attempts(chain: Chain) -> None:
    chain.primary.behaviour = Behaviour(status=502, body={"error": {"message": "gone"}})
    chain.secondary.behaviour = Behaviour(chunks=[chunk("hello")])

    await chain.send(stream=True)
    row = await chain.row()

    assert [attempt["model_name"] for attempt in row.failover_attempts] == [
        "primary",
        "secondary",
    ]
    assert row.failed_after_stream_start is False


# ---------------------------------------------------------------------------
# A/B split
# ---------------------------------------------------------------------------


async def test_a_fixed_user_always_lands_on_the_same_variant(chain: Chain) -> None:
    """The acceptance criterion, over the real route: sticky assignment means an end user
    sees one variant, so a comparison is between models rather than between coin flips."""
    first, second = chain.targets
    chain.route("ab_split", weights={first.id: 50, second.id: 50})
    chain.primary.behaviour = Behaviour(body=completion("a"))
    chain.secondary.behaviour = Behaviour(body=completion("b"))

    served = set()
    for _ in range(100):
        response = await chain.send(user="stable-person")
        served.add(response.headers["X-Gateway-Model"])

    assert len(served) == 1


async def test_the_header_identifies_the_user_when_the_body_does_not(chain: Chain) -> None:
    """``X-Gateway-User`` is set by the customer's own backend and survives a client
    library that drops unknown body fields."""
    first, second = chain.targets
    chain.route("ab_split", weights={first.id: 50, second.id: 50})
    chain.primary.behaviour = Behaviour(body=completion("a"))
    chain.secondary.behaviour = Behaviour(body=completion("b"))

    served = set()
    for _ in range(60):
        response = await chain.client.post(
            "/g/demo/v1/chat/completions",
            json={"model": "demo", "messages": [{"role": "user", "content": "hi"}]},
            headers={
                "Authorization": f"Bearer {chain.token}",
                "X-Gateway-User": "person-from-the-header",
            },
        )
        served.add(response.headers["X-Gateway-Model"])

    assert len(served) == 1


async def test_without_a_user_both_variants_are_used(chain: Chain) -> None:
    first, second = chain.targets
    chain.route("ab_split", weights={first.id: 50, second.id: 50})
    chain.primary.behaviour = Behaviour(body=completion("a"))
    chain.secondary.behaviour = Behaviour(body=completion("b"))

    served = {(await chain.send()).headers["X-Gateway-Model"] for _ in range(60)}

    assert served == {"primary", "secondary"}


async def test_an_ab_failure_reaches_the_client_rather_than_the_other_variant(
    chain: Chain,
) -> None:
    """SPEC §8.1, and the reason it is a rule rather than a preference: a retry would move
    this request to the other arm and quietly bias the experiment."""
    first, second = chain.targets
    chain.route("ab_split", weights={first.id: 100, second.id: 0})
    chain.primary.behaviour = Behaviour(status=503, body={"error": {"message": "down"}})

    response = await chain.send(user="someone")

    assert response.status_code == 503
    assert chain.secondary.requests == []


async def test_the_row_records_which_variant_served(chain: Chain) -> None:
    """What makes the comparison possible at all: without the model on the row there is
    nothing to group the latency and error numbers by."""
    first, second = chain.targets
    chain.route("ab_split", weights={first.id: 0, second.id: 100})
    chain.secondary.behaviour = Behaviour(body=completion("b"))

    await chain.send(user="someone")
    row = await chain.row()

    assert row.model_name == "secondary"
    assert row.upstream_model_id == second.id
    # One target was involved, so there is no timeline — the row says it all.
    assert row.failover_attempts == []


def _frames(body: str) -> list[str]:
    return [
        line.removeprefix("data: ")
        for line in body.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
