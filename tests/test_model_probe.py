"""What "Test connection" reports, against a real provider on a real port.

The scripted upstream is the same one the proxy tests use, which is the point: the probe
goes through the production adapter, so if it says a model works, a real completion
through the gateway sends the same headers to the same URL. A probe that built its own
request would happily pass for a model that 401s in production.

The four cases the task names — success, a bad key, a bad URL, and a timeout — are here,
plus the one that is easy to get wrong: a 200 whose body is not a completion.
"""

from __future__ import annotations

import httpx
import pytest

from app.adapters.base import UpstreamTarget
from app.core.ids import uuid7
from app.services.model_probe import MAX_PROBE_SECONDS, PROBE_REQUEST, ModelProbe
from tests.support import Behaviour, MockUpstream, completion


@pytest.fixture
async def probe():  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(timeout=None) as http:
        yield ModelProbe(http)


def target(base_url: str, **overrides: object) -> UpstreamTarget:
    values: dict[str, object] = {
        "id": uuid7(),
        "name": "candidate",
        "base_url": base_url,
        "dialect": "openai",
        "upstream_model_id": "gpt-4o-mini",
        "auth_type": "bearer",
        "credential": "sk-probe-key",
        "timeout_seconds": 2,
    }
    values.update(overrides)
    return UpstreamTarget(**values)  # type: ignore[arg-type]


async def test_a_working_model_reports_ok_and_a_latency(
    upstream: MockUpstream, probe: ModelProbe
) -> None:
    upstream.behaviour = Behaviour(body=completion(model="gpt-4o-mini-2024"))

    result = await probe.run(target(f"{upstream.base_url}/v1"))

    assert result.ok is True
    assert result.upstream_status == 200
    assert result.latency_ms >= 0
    assert result.model_echo == "gpt-4o-mini-2024"


async def test_the_probe_goes_to_the_chat_completions_endpoint(
    upstream: MockUpstream, probe: ModelProbe
) -> None:
    upstream.behaviour = Behaviour(body=completion())

    await probe.run(target(f"{upstream.base_url}/v1"))

    assert upstream.last_request.path == "/v1/chat/completions"


async def test_the_probe_carries_the_credential_the_real_path_would(
    upstream: MockUpstream, probe: ModelProbe
) -> None:
    upstream.behaviour = Behaviour(body=completion())

    await probe.run(target(f"{upstream.base_url}/v1"))

    assert upstream.last_request.headers["authorization"] == "Bearer sk-probe-key"


async def test_the_probe_sends_the_configured_model_not_the_placeholder(
    upstream: MockUpstream, probe: ModelProbe
) -> None:
    upstream.behaviour = Behaviour(body=completion())

    await probe.run(target(f"{upstream.base_url}/v1"))

    assert upstream.last_request.body["model"] == "gpt-4o-mini"


async def test_the_probe_costs_one_token(upstream: MockUpstream, probe: ModelProbe) -> None:
    """Negligible by construction, which is what makes it safe behind a button people
    press repeatedly while they get the URL right."""
    upstream.behaviour = Behaviour(body=completion())

    await probe.run(target(f"{upstream.base_url}/v1"))

    assert upstream.last_request.body["max_tokens"] == 1
    assert upstream.last_request.body["stream"] is False
    assert len(upstream.last_request.body["messages"]) == 1


async def test_the_probe_request_is_not_mutated_between_calls(
    upstream: MockUpstream, probe: ModelProbe
) -> None:
    """It is a module-level constant, so anything that edited it in place would corrupt
    every later probe in the process."""
    upstream.behaviour = Behaviour(body=completion())

    await probe.run(target(f"{upstream.base_url}/v1"))

    assert PROBE_REQUEST.model == "probe"
    assert PROBE_REQUEST.max_tokens == 1


async def test_a_bad_key_reports_the_upstream_error_verbatim(
    upstream: MockUpstream, probe: ModelProbe
) -> None:
    upstream.behaviour = Behaviour(
        status=401,
        body={
            "error": {
                "message": "Incorrect API key provided: sk-wro***key.",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        },
    )

    result = await probe.run(target(f"{upstream.base_url}/v1"))

    assert result.ok is False
    assert result.upstream_status == 401
    assert result.error_message == "invalid_api_key Incorrect API key provided: sk-wro***key."


async def test_a_wrong_path_reports_the_upstream_404(
    upstream: MockUpstream, probe: ModelProbe
) -> None:
    """The commonest misconfiguration by a distance: a base URL missing ``/v1``."""
    upstream.behaviour = Behaviour(
        status=404, body={"error": {"message": "Unrecognized request URL.", "code": "unknown_url"}}
    )

    result = await probe.run(target(upstream.base_url))

    assert result.ok is False
    assert result.upstream_status == 404
    assert "Unrecognized request URL." in (result.error_message or "")


async def test_an_unreachable_host_reports_the_transport_error(probe: ModelProbe) -> None:
    """No status, because the request never got one. The verbatim transport error is
    what distinguishes a typo in the host from a firewall in the way."""
    result = await probe.run(target("http://127.0.0.1:1/v1"))  # nothing listens on port 1

    assert result.ok is False
    assert result.upstream_status is None
    assert "Error" in (result.error_message or "")


async def test_a_hanging_provider_times_out_and_says_so(
    upstream: MockUpstream, probe: ModelProbe
) -> None:
    upstream.behaviour = Behaviour(hang=True)

    result = await probe.run(target(f"{upstream.base_url}/v1", timeout_seconds=1))

    assert result.ok is False
    assert result.upstream_status is None
    assert result.error_message == "No response within 1s."


async def test_a_two_hundred_that_is_not_a_completion_is_not_ok(
    upstream: MockUpstream, probe: ModelProbe
) -> None:
    """A proxy or a captive portal answering 200 with an HTML page. Reporting "OK" here
    would send the operator away believing a model works that cannot serve a request."""
    upstream.behaviour = Behaviour(body={"hello": "world"})

    result = await probe.run(target(f"{upstream.base_url}/v1"))

    assert result.ok is False
    assert result.upstream_status == 200
    assert "not a chat completion" in (result.error_message or "")


async def test_an_unregistered_dialect_fails_without_a_request(
    upstream: MockUpstream, probe: ModelProbe
) -> None:
    """A dialect this build has no adapter for. The service refuses one at write time;
    this is the belt to that pair of braces, for a model stored before its dialect lost —
    or never had — an adapter."""
    result = await probe.run(target(f"{upstream.base_url}/v1", dialect="bedrock"))

    assert result.ok is False
    assert "no adapter" in (result.error_message or "")
    assert upstream.requests == []


async def test_the_read_budget_is_capped_below_the_models_timeout() -> None:
    """A 600-second model timeout is a legitimate setting for a slow reasoning model, and
    a terrible amount of time to hold a browser tab open on a health check."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=completion())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, timeout=None) as http:
        await ModelProbe(http).run(target("https://provider.example/v1", timeout_seconds=600))

    assert seen[0].extensions["timeout"]["read"] == MAX_PROBE_SECONDS
