"""``POST /gateways/{id}/test``, against a real provider on a real port.

The fake in ``tests/gateway_support.py`` answers "did the service ask for the right
slug". This file answers the question that actually matters: **does the probe do what a
customer's request does?** So it runs the production resolver output through the
production :class:`~app.services.proxy.ProxyService` and the production adapter, over a
socket, and inspects what the provider received.

If a test here and one in ``tests/test_proxy_chat.py`` ever disagree about the same
configuration, the debugging button has started lying — which is worse than not having it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.services.gateway_probe import PROBE_MAX_TOKENS, ProxyGatewayProbe
from app.services.gateway_resolver import ResolvedGateway
from app.services.proxy import ProxyService
from tests.support import (
    Behaviour,
    FakeResolver,
    MockUpstream,
    completion,
    make_gateway,
    make_target,
)


@dataclasses.dataclass
class Harness:
    probe: ProxyGatewayProbe
    resolver: FakeResolver
    upstream: MockUpstream

    @property
    def gateway(self) -> ResolvedGateway:
        return self.resolver.gateway

    def reconfigure(self, **overrides: Any) -> None:
        self.resolver.gateway = dataclasses.replace(self.gateway, **overrides)

    def retarget(self, **overrides: Any) -> None:
        target = dataclasses.replace(self.gateway.targets[0], **overrides)
        self.resolver.gateway = dataclasses.replace(self.gateway, targets=(target,))


@pytest.fixture
async def harness(upstream: MockUpstream) -> AsyncIterator[Harness]:
    resolver = FakeResolver(gateway=make_gateway(make_target(f"{upstream.base_url}/v1")))
    async with httpx.AsyncClient(timeout=10.0) as http:
        yield Harness(
            probe=ProxyGatewayProbe(resolver, ProxyService(http)),
            resolver=resolver,
            upstream=upstream,
        )


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


async def test_a_working_gateway_reports_the_answer(harness: Harness) -> None:
    harness.upstream.behaviour = Behaviour(body=completion("I am here."))

    result = await harness.probe.run("demo", message="are you there?")

    assert result.ok is True
    assert result.content == "I am here."
    assert result.model_name == "demo-upstream"


async def test_the_probe_reports_where_the_time_went(harness: Harness) -> None:
    """The gap between the two is what this gateway costs. Tasks 10 and 12 add retrieval
    to the same breakdown, which is why it is a breakdown and not one number."""
    harness.upstream.behaviour = Behaviour(body=completion())

    result = await harness.probe.run("demo", message="hi")

    assert result.total_ms >= result.upstream_ms >= 0


async def test_the_probe_goes_to_the_real_provider_endpoint(harness: Harness) -> None:
    harness.upstream.behaviour = Behaviour(body=completion())

    await harness.probe.run("demo", message="hi")

    assert harness.upstream.last_request.path == "/v1/chat/completions"
    assert harness.upstream.last_request.headers["authorization"] == "Bearer sk-upstream-secret"


async def test_the_probe_translates_the_virtual_model_name(harness: Harness) -> None:
    """Exactly as a customer request does: the client says the slug, the provider is
    asked for its own model id."""
    harness.upstream.behaviour = Behaviour(body=completion())

    await harness.probe.run("demo", message="hi")

    assert harness.upstream.last_request.body["model"] == "upstream-model"


async def test_the_probe_is_not_streamed(harness: Harness) -> None:
    harness.upstream.behaviour = Behaviour(body=completion())

    await harness.probe.run("demo", message="hi")

    assert harness.upstream.last_request.body.get("stream") in (False, None)


async def test_the_probe_bounds_what_it_spends(harness: Harness) -> None:
    """Small enough to press repeatedly, large enough that the answer is a real
    completion rather than an empty one hiding a broken prompt."""
    harness.upstream.behaviour = Behaviour(body=completion())

    await harness.probe.run("demo", message="hi")

    assert harness.upstream.last_request.body["max_tokens"] == PROBE_MAX_TOKENS


# ---------------------------------------------------------------------------
# the assembled prompt
# ---------------------------------------------------------------------------


async def test_the_assembled_prompt_is_returned(harness: Harness) -> None:
    """The part that makes this a debugging tool. Before task 07's request log, it is the
    only way to see what the system context became."""
    harness.reconfigure(system_context="You are Acme's support assistant. Be concise.")
    harness.upstream.behaviour = Behaviour(body=completion())

    result = await harness.probe.run("demo", message="how do I reset my password?")

    assert [message.role for message in result.assembled_prompt] == ["system", "user"]
    assert "Acme's support assistant" in result.assembled_prompt[0].content
    assert result.assembled_prompt[1].content == "how do I reset my password?"


async def test_the_returned_prompt_is_the_one_that_was_sent(harness: Harness) -> None:
    """The assertion that keeps the button honest: what the panel shows and what the
    provider received are compared against each other, not both against an expectation."""
    harness.reconfigure(system_context="Gateway rules.")
    harness.retarget(system_context="Model rules.")
    harness.upstream.behaviour = Behaviour(body=completion())

    result = await harness.probe.run("demo", message="hi")

    sent = harness.upstream.last_request.body["messages"]
    assert [message.content for message in result.assembled_prompt] == [
        message["content"] for message in sent
    ]


async def test_the_layers_are_ordered_model_then_gateway(harness: Harness) -> None:
    harness.reconfigure(system_context="Gateway rules.")
    harness.retarget(system_context="Model rules.")
    harness.upstream.behaviour = Behaviour(body=completion())

    result = await harness.probe.run("demo", message="hi")

    assert result.assembled_prompt[0].content == "Model rules.\n\nGateway rules."


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------


async def test_the_gateways_parameters_are_applied(harness: Harness) -> None:
    harness.reconfigure(param_overrides={"temperature": 0.15})
    harness.upstream.behaviour = Behaviour(body=completion())

    await harness.probe.run("demo", message="hi")

    assert harness.upstream.last_request.body["temperature"] == 0.15


async def test_a_lock_on_max_tokens_is_reported(harness: Harness) -> None:
    """The probe asks for ``max_tokens``, so a gateway that pins it overrides the probe
    exactly as it would override a customer — and says so, in the same words."""
    harness.reconfigure(locked_params={"max_tokens": 4})
    harness.upstream.behaviour = Behaviour(body=completion())

    result = await harness.probe.run("demo", message="hi")

    assert harness.upstream.last_request.body["max_tokens"] == 4
    assert result.locked_overrides == ("max_tokens",)


# ---------------------------------------------------------------------------
# failures are results, not exceptions
# ---------------------------------------------------------------------------


async def test_an_upstream_401_is_a_result_not_an_error(harness: Harness) -> None:
    """ "The upstream said 401" is the successful answer to "does this gateway work"."""
    harness.upstream.behaviour = Behaviour(
        status=401,
        body={"error": {"message": "Incorrect API key provided.", "code": "invalid_api_key"}},
    )

    result = await harness.probe.run("demo", message="hi")

    assert result.ok is False
    assert result.upstream_status == 401
    assert "Incorrect API key provided." in (result.error_message or "")


async def test_a_failure_still_shows_the_prompt_that_was_attempted(harness: Harness) -> None:
    """The most useful moment for the prompt panel is when the call failed."""
    harness.reconfigure(system_context="Be concise.")
    harness.upstream.behaviour = Behaviour(status=500, body={"error": {"message": "boom"}})

    result = await harness.probe.run("demo", message="hi")

    assert result.ok is False
    assert [message.role for message in result.assembled_prompt] == ["system", "user"]


async def test_a_gateway_with_no_target_says_what_to_do(harness: Harness) -> None:
    harness.reconfigure(targets=())

    result = await harness.probe.run("demo", message="hi")

    assert result.ok is False
    assert "no upstream model configured" in (result.error_message or "")


async def test_a_disabled_model_is_named(harness: Harness) -> None:
    harness.reconfigure(targets=(), disabled=("acme-gpt",))

    result = await harness.probe.run("demo", message="hi")

    assert result.ok is False
    assert "'acme-gpt'" in (result.error_message or "")


async def test_a_disabled_gateway_is_a_result_too(harness: Harness) -> None:
    from app.api.proxy.errors import GatewayDisabled

    harness.resolver.error = GatewayDisabled("Gateway 'demo' is disabled.")

    result = await harness.probe.run("demo", message="hi")

    assert result.ok is False
    assert "disabled" in (result.error_message or "")


async def test_an_unreachable_upstream_is_a_result(harness: Harness) -> None:
    harness.retarget(base_url="http://127.0.0.1:1/v1")

    result = await harness.probe.run("demo", message="hi")

    assert result.ok is False
    assert result.upstream_status is None


async def test_a_long_message_is_truncated_rather_than_refused(harness: Harness) -> None:
    """The schema caps it first; this is the belt behind that, so a direct caller cannot
    turn the debugging button into a way to send an unbounded prompt."""
    harness.upstream.behaviour = Behaviour(body=completion())

    await harness.probe.run("demo", message="x" * 10_000)

    sent = harness.upstream.last_request.body["messages"][0]["content"]
    assert len(sent) == 2000
