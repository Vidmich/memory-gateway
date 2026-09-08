"""An Anthropic upstream through the whole stack, over a real socket.

``tests/test_adapter_anthropic.py`` proves the translation by calling it and
``tests/test_adapter_contract.py`` proves the two dialects agree. This file proves the
part neither can: that a request entering the gateway as OpenAI leaves as Anthropic and
comes back as OpenAI, with the status codes, the headers and the log row that a real
client and a real operator see.

The demo the task names is here twice — once for a Claude upstream on its own, and once
for a 50/50 A/B across dialects, where the point is that nothing downstream can tell which
provider answered.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.adapters.base import UpstreamTarget
from app.api.proxy.deps import get_router
from app.db.models import RequestLog
from app.services.proxy import ProxyService
from app.services.routing import Router
from tests.conftest import ProxyHarness, build_proxy_app
from tests.monitoring_support import LogFixture, build_logs
from tests.support import (
    Behaviour,
    FakeAuthenticator,
    FakeResolver,
    MockUpstream,
    anthropic_events,
    anthropic_message,
    completion,
    make_gateway,
    make_target,
    serve,
)

pytestmark = pytest.mark.anyio

BODY: dict[str, Any] = {"model": "demo", "messages": [{"role": "user", "content": "hi"}]}


def frames_of(text: str) -> list[str]:
    return [line[6:] for line in text.splitlines() if line.startswith("data: ")]


async def post(proxy: ProxyHarness, **overrides: Any) -> Any:
    payload = {**BODY, **overrides}
    return await proxy.client.post(proxy.url(), json=payload, headers=proxy.headers())


@pytest.fixture
async def claude(live_proxy: ProxyHarness) -> ProxyHarness:
    """The standard harness with its one upstream switched to the anthropic dialect.

    Nothing else changes — same gateway, same key, same route — which is the point being
    demonstrated: a dialect is a property of the model, not of the endpoint.
    """
    live_proxy.retarget(dialect="anthropic", auth_type="api_key_header")
    return live_proxy


# ---------------------------------------------------------------------------
# the demo: an unmodified OpenAI client talking to Claude
# ---------------------------------------------------------------------------


async def test_a_completion_comes_back_openai_shaped(claude: ProxyHarness) -> None:
    claude.upstream.behaviour = Behaviour(body=anthropic_message("hello from Claude"))

    response = await post(claude)
    body = response.json()

    assert response.status_code == 200
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hello from Claude"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


async def test_the_request_goes_to_the_messages_endpoint_with_its_key(
    claude: ProxyHarness,
) -> None:
    claude.upstream.behaviour = Behaviour(body=anthropic_message())

    await post(claude)

    sent = claude.upstream.last_request
    assert sent.path == "/v1/messages"
    assert sent.headers["x-api-key"] == "sk-upstream-secret"
    assert sent.headers["anthropic-version"]


async def test_the_system_layers_become_the_system_parameter(claude: ProxyHarness) -> None:
    """The single most important difference, end to end: the model's configured context,
    the gateway's, and the client's own all reach ``system`` — and none of them is left in
    ``messages``, where Anthropic would reject or silently demote it."""
    claude.retarget(dialect="anthropic", system_context="You are terse.")
    claude.resolver.gateway = replace(claude.gateway, system_context="Answer in English.")
    claude.upstream.behaviour = Behaviour(body=anthropic_message())

    await post(claude, messages=[{"role": "system", "content": "Be formal."}, BODY["messages"][0]])

    sent = claude.upstream.last_request.body
    assert sent["system"] == "You are terse.\n\nAnswer in English.\n\nBe formal."
    assert [turn["role"] for turn in sent["messages"]] == ["user"]


async def test_max_tokens_is_always_on_the_outbound_request(claude: ProxyHarness) -> None:
    """A client that never sends one — which is most of them — would otherwise get a 400
    from the provider on every single request."""
    claude.upstream.behaviour = Behaviour(body=anthropic_message())

    await post(claude)

    assert claude.upstream.last_request.body["max_tokens"] > 0


async def test_a_models_default_params_reach_the_translated_request(
    claude: ProxyHarness,
) -> None:
    claude.retarget(dialect="anthropic", default_params={"max_tokens": 77, "temperature": 1.5})
    claude.upstream.behaviour = Behaviour(body=anthropic_message())

    await post(claude)

    sent = claude.upstream.last_request.body
    assert sent["max_tokens"] == 77
    # Clamped on the way through: the merge produced a legal OpenAI value that is not a
    # legal Anthropic one.
    assert sent["temperature"] == 1.0


async def test_a_locked_parameter_is_still_locked_through_a_translation(
    claude: ProxyHarness,
) -> None:
    """The gateway's policy layer sits above the dialect, and has to keep working when the
    dialect rewrites the request underneath it."""
    claude.resolver.gateway = replace(claude.gateway, locked_params={"temperature": 0.2})
    claude.upstream.behaviour = Behaviour(body=anthropic_message())

    response = await post(claude, temperature=0.9)

    assert claude.upstream.last_request.body["temperature"] == 0.2
    assert response.headers["x-gateway-locked-params"] == "temperature"


async def test_asking_for_several_completions_is_a_clear_400(claude: ProxyHarness) -> None:
    """Refused before anything is sent, and in the client's own error envelope."""
    response = await post(claude, n=3)

    assert response.status_code == 400
    assert claude.upstream.requests == []
    error = response.json()["error"]
    assert error["param"] == "n"
    assert "one completion per request" in error["message"]


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


async def test_a_stream_arrives_as_openai_chunks(claude: ProxyHarness) -> None:
    claude.upstream.behaviour = Behaviour(chunks=anthropic_events("one ", "two"), send_done=False)

    response = await post(claude, stream=True)
    frames = frames_of(response.text)

    assert response.status_code == 200
    assert frames[-1] == "[DONE]"
    payloads = [json.loads(frame) for frame in frames[:-1]]
    assert all(payload["object"] == "chat.completion.chunk" for payload in payloads)
    content = "".join(
        choice["delta"].get("content") or ""
        for payload in payloads
        for choice in payload["choices"]
    )
    assert content == "one two"
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"


async def test_pings_never_reach_the_client(claude: ProxyHarness) -> None:
    claude.upstream.behaviour = Behaviour(chunks=anthropic_events("hi"), send_done=False)

    response = await post(claude, stream=True)

    assert "ping" not in response.text


async def test_a_truncated_stream_says_length(claude: ProxyHarness) -> None:
    claude.upstream.behaviour = Behaviour(
        chunks=anthropic_events("as far as it g", stop_reason="max_tokens"), send_done=False
    )

    response = await post(claude, stream=True)
    last = json.loads(frames_of(response.text)[-2])

    assert last["choices"][0]["finish_reason"] == "length"


async def test_a_streamed_response_is_logged_the_same_way_a_relayed_one_is(
    claude: ProxyHarness,
) -> None:
    """Task 07 reassembles a streamed completion from its deltas; a translating dialect
    must produce deltas that reassemble to the same text."""
    claude.upstream.behaviour = Behaviour(chunks=anthropic_events("one ", "two"), send_done=False)

    await post(claude, stream=True)
    await claude.logs.flush()

    row = claude.logs.rows[-1]
    transcript = claude.logs.transcript(row.id)
    assert transcript is not None
    assert transcript.response_body == "one two"


async def test_an_error_event_mid_stream_ends_the_stream_and_marks_the_row(
    claude: ProxyHarness,
) -> None:
    """SPEC §8.2's unrecoverable case, reached the way Anthropic reaches it: not a dropped
    connection but a provider that says, several frames in, that it has given up. The 200
    is already on the wire, so the only honest ending is a terminating error frame."""
    events = anthropic_events("half a se")[:4]
    events.append(
        json.dumps(
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
        )
    )
    claude.upstream.behaviour = Behaviour(chunks=events, send_done=False)

    response = await post(claude, stream=True)
    await claude.logs.flush()
    row = claude.logs.rows[-1]

    assert response.status_code == 200
    # The error frame is the last one: a stream that failed does not get a `[DONE]`, which
    # is how a client tells "the answer ended" from "the answer stopped".
    assert json.loads(frames_of(response.text)[-1])["error"]["code"] == "upstream_error"
    assert row.error_code == "stream_failed"
    assert row.failed_after_stream_start is True


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


async def test_a_provider_error_reaches_the_client_in_its_own_envelope(
    claude: ProxyHarness,
) -> None:
    claude.upstream.behaviour = Behaviour(
        status=401,
        body={
            "type": "error",
            "error": {"type": "authentication_error", "message": "invalid x-api-key"},
        },
    )

    response = await post(claude)

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["type"] == "authentication_error"
    assert "invalid x-api-key" in error["message"]


async def test_an_overloaded_provider_reaches_the_client_as_a_503(claude: ProxyHarness) -> None:
    """Anthropic's 529 has no meaning to a client SDK, and none to SPEC §8.2's retry
    table either. It arrives as the retryable 503 it means."""
    claude.upstream.behaviour = Behaviour(
        status=529,
        body={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    )

    response = await post(claude)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "overloaded_error"


# ---------------------------------------------------------------------------
# the dropped set, on the row
# ---------------------------------------------------------------------------


async def test_dropped_parameters_are_recorded_on_the_request_log(
    claude: ProxyHarness,
) -> None:
    """The support ticket this prevents: "I set presence_penalty and nothing changed."
    The request succeeds, so nothing else anywhere would have said so."""
    claude.upstream.behaviour = Behaviour(body=anthropic_message())

    await post(claude, presence_penalty=0.5, seed=7, temperature=0.4)
    await claude.logs.flush()

    assert claude.logs.rows[-1].dropped_params == ["presence_penalty", "seed"]


async def test_an_openai_upstream_drops_nothing(live_proxy: ProxyHarness) -> None:
    """The other half of the same fact: the column is empty for the dialect that forwards
    everything, so a non-empty one always means a translation happened."""
    live_proxy.upstream.behaviour = Behaviour(body=completion())

    await live_proxy.client.post(
        live_proxy.url(), json={**BODY, "presence_penalty": 0.5}, headers=live_proxy.headers()
    )
    await live_proxy.logs.flush()

    assert live_proxy.logs.rows[-1].dropped_params == []


async def test_a_models_own_default_is_recorded_as_dropped_too(claude: ProxyHarness) -> None:
    """An operator who put ``frequency_penalty`` in a Claude model's defaults has
    configured something that never happens, and this is where they find out."""
    claude.retarget(dialect="anthropic", default_params={"frequency_penalty": 0.3})
    claude.upstream.behaviour = Behaviour(body=anthropic_message())

    await post(claude)
    await claude.logs.flush()

    assert claude.logs.rows[-1].dropped_params == ["frequency_penalty"]


# ---------------------------------------------------------------------------
# the A/B demo: two dialects, one schema
# ---------------------------------------------------------------------------


@pytest.fixture
async def split(
    upstream: MockUpstream,
) -> AsyncIterator[tuple[AsyncClient, str, LogFixture, Sequence[UpstreamTarget]]]:
    """A gateway split 50/50 between an OpenAI upstream and an Anthropic one."""
    anthropic = MockUpstream()
    async with serve(anthropic) as base_url:
        anthropic.base_url = base_url

        first = make_target(f"{upstream.base_url}/v1", name="gpt", timeout_seconds=2)
        second = make_target(
            f"{anthropic.base_url}/v1",
            name="claude",
            dialect="anthropic",
            auth_type="api_key_header",
            timeout_seconds=2,
        )
        gateway = make_gateway(
            first,
            targets=(first, second),
            routing_mode="ab_split",
            weights={first.id: 50, second.id: 50},
        )

        resolver = FakeResolver(gateway=gateway)
        authenticator = FakeAuthenticator()
        token = authenticator.issue(gateway.id)
        logs = build_logs()
        application = build_proxy_app(resolver, authenticator, logs)
        _no_backoff(application)

        upstream.behaviour = Behaviour(body=completion("from gpt"))
        anthropic.behaviour = Behaviour(body=anthropic_message("from claude"))

        async with application.router.lifespan_context(application):
            transport = ASGITransport(app=application)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                yield client, token, logs, gateway.targets


def _no_backoff(application: FastAPI) -> None:
    def build() -> Router:
        return Router(ProxyService(application.state.clients.http), backoff=lambda: 0.0)

    application.dependency_overrides[get_router] = build


async def test_a_fifty_fifty_split_across_dialects_is_invisible_to_the_client(
    split: tuple[AsyncClient, str, LogFixture, Sequence[UpstreamTarget]],
) -> None:
    """The acceptance criterion, stated literally: a hundred requests, both providers
    serving, and every response the same shape.

    The comparison is on the *shape* rather than the content — the completions differ, and
    so do the model names, because those are the two things that are supposed to differ.
    """
    client, token, _, _ = split

    shapes: set[str] = set()
    models: set[str] = set()
    for index in range(100):
        response = await client.post(
            "/g/demo/v1/chat/completions",
            json={**BODY, "user": f"person-{index}"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        body = response.json()
        shapes.add(json.dumps(_shape(body), sort_keys=True))
        models.add(response.headers["x-gateway-model"])

    assert models == {"gpt", "claude"}, "the split did not reach both dialects"
    assert len(shapes) == 1, shapes


async def test_the_split_is_visible_in_the_monitoring_rows(
    split: tuple[AsyncClient, str, LogFixture, Sequence[UpstreamTarget]],
) -> None:
    """What the monitoring page draws its per-model breakdown from. Both dialects have to
    land in the same table with the same fields filled in, or half the chart is missing."""
    client, token, logs, _ = split

    for index in range(40):
        await client.post(
            "/g/demo/v1/chat/completions",
            json={**BODY, "user": f"person-{index}"},
            headers={"Authorization": f"Bearer {token}"},
        )
    await logs.flush()

    served = {row.model_name for row in logs.rows}
    assert served == {"gpt", "claude"}
    assert all(row.status_code == 200 for row in logs.rows)
    assert all(row.prompt_tokens is not None for row in logs.rows)


def _shape(body: Any) -> Any:
    """The structure of a response with its values replaced by their types."""
    if isinstance(body, dict):
        return {key: _shape(value) for key, value in sorted(body.items())}
    if isinstance(body, list):
        return [_shape(item) for item in body]
    return type(body).__name__


# ---------------------------------------------------------------------------
# the abstraction claim
# ---------------------------------------------------------------------------


def test_the_data_plane_route_knows_nothing_about_dialects() -> None:
    """The acceptance criterion that cannot be written as a behaviour: adding a dialect
    required no change to ``app/api/proxy/``.

    Enforced by reading the files, because the claim is about coupling rather than output
    — a route that grew an ``if dialect == "anthropic"`` would pass every other test in
    this file while making the next dialect somebody's afternoon.
    """
    route = Path(__file__).resolve().parent.parent / "app" / "api" / "proxy"
    offenders = {
        path.name: word
        for path in sorted(route.glob("*.py"))
        for word in ("anthropic", "dialect", "claude")
        if word in path.read_text(encoding="utf-8").lower()
    }

    assert offenders == {}, f"the proxy route has grown dialect knowledge: {offenders}"


async def test_the_request_log_row_looks_the_same_whichever_dialect_served(
    claude: ProxyHarness,
) -> None:
    """Every field task 07 and task 14 read, filled in identically. A dialect that left
    ``prompt_tokens`` null would silently switch off token limiting for that model."""
    claude.upstream.behaviour = Behaviour(
        body=anthropic_message("hi", input_tokens=11, output_tokens=4)
    )

    await post(claude)
    await claude.logs.flush()
    row: RequestLog = claude.logs.rows[-1]

    assert row.status_code == 200
    assert row.model_name == "demo-upstream"
    assert (row.prompt_tokens, row.completion_tokens) == (11, 4)
    assert row.latency_upstream_ms is not None
    assert isinstance(row.upstream_model_id, uuid.UUID)
