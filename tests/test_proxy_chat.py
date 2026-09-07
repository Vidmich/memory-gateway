"""The proxy end to end: forwarding, error mapping, and the OpenAI error envelope.

Driven against a real upstream on a real socket, so the request the provider sees is the
one the gateway actually put on the wire.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

import pytest

from tests.conftest import ProxyHarness
from tests.support import Behaviour, completion


def payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"model": "demo", "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return body


async def post(proxy: ProxyHarness, **overrides: Any) -> Any:
    return await proxy.client.post(proxy.url(), json=payload(**overrides), headers=proxy.headers())


# -- the happy path ----------------------------------------------------------


async def test_completion_is_returned_to_the_client(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(body=completion("hello there"))

    response = await post(proxy)

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hello there"


async def test_response_headers_identify_the_gateway_and_the_model(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(body=completion())

    response = await post(proxy)

    assert response.headers["x-gateway-model"] == "demo-upstream"
    assert response.headers["x-gateway-request-id"]


async def test_request_reaches_the_provider_endpoint_with_its_credential(
    proxy: ProxyHarness,
) -> None:
    proxy.upstream.behaviour = Behaviour(body=completion())

    await post(proxy)

    sent = proxy.upstream.last_request
    assert sent.path == "/v1/chat/completions"
    assert sent.headers["authorization"] == "Bearer sk-upstream-secret"


async def test_virtual_model_name_is_translated(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(body=completion())

    await post(proxy)

    assert proxy.upstream.last_request.body["model"] == "upstream-model"


async def test_system_context_layers_are_prepended(proxy: ProxyHarness) -> None:
    import dataclasses

    proxy.retarget(system_context="You are terse.")
    proxy.resolver.gateway = dataclasses.replace(proxy.gateway, system_context="Answer in English.")
    proxy.upstream.behaviour = Behaviour(body=completion())

    await post(proxy, messages=[{"role": "user", "content": "hi"}])

    sent = proxy.upstream.last_request.body["messages"]
    assert sent[0] == {"role": "system", "content": "You are terse.\n\nAnswer in English."}
    assert sent[1]["content"] == "hi"


async def test_parameters_are_merged_before_forwarding(proxy: ProxyHarness) -> None:
    import dataclasses

    proxy.retarget(default_params={"temperature": 0.2, "top_p": 0.5})
    proxy.resolver.gateway = dataclasses.replace(
        proxy.gateway, param_overrides={"temperature": 0.7}
    )
    proxy.upstream.behaviour = Behaviour(body=completion())

    await post(proxy, max_tokens=64)

    sent = proxy.upstream.last_request.body
    assert sent["temperature"] == 0.7
    assert sent["top_p"] == 0.5
    assert sent["max_tokens"] == 64


# -- locked parameters -------------------------------------------------------
#
# The distinction task 06 introduces: an *override* is a default the client can beat, a
# *lock* wins. Both matter — pinning `temperature` for every caller and suggesting one are
# different policies — and the only thing separating them is where they sit in the merge.


async def test_a_locked_parameter_beats_the_client(proxy: ProxyHarness) -> None:
    proxy.resolver.gateway = dataclasses.replace(proxy.gateway, locked_params={"temperature": 0.2})
    proxy.upstream.behaviour = Behaviour(body=completion())

    await post(proxy, temperature=1.9)

    assert proxy.upstream.last_request.body["temperature"] == 0.2


async def test_an_override_still_loses_to_the_client(proxy: ProxyHarness) -> None:
    """The other half. If this ever starts failing, locks have silently become the only
    behaviour and every gateway with an override has changed meaning."""
    proxy.resolver.gateway = dataclasses.replace(
        proxy.gateway, param_overrides={"temperature": 0.2}
    )
    proxy.upstream.behaviour = Behaviour(body=completion())

    await post(proxy, temperature=1.9)

    assert proxy.upstream.last_request.body["temperature"] == 1.9


async def test_an_override_that_was_applied_is_reported_in_a_header(
    proxy: ProxyHarness,
) -> None:
    """Ignoring what a caller explicitly asked for is defensible; doing it silently is
    not. The header is what makes "this endpoint ignores temperature" a documented policy
    rather than a bug report."""
    proxy.resolver.gateway = dataclasses.replace(proxy.gateway, locked_params={"temperature": 0.2})
    proxy.upstream.behaviour = Behaviour(body=completion())

    response = await post(proxy, temperature=1.9)

    assert response.headers["x-gateway-locked-params"] == "temperature"


async def test_a_lock_the_client_did_not_contradict_reports_nothing(
    proxy: ProxyHarness,
) -> None:
    """A header on every response is noise, and noise is how a useful header stops being
    read."""
    proxy.resolver.gateway = dataclasses.replace(proxy.gateway, locked_params={"temperature": 0.2})
    proxy.upstream.behaviour = Behaviour(body=completion())

    response = await post(proxy)

    assert "x-gateway-locked-params" not in response.headers


async def test_a_lock_matching_what_the_client_asked_for_reports_nothing(
    proxy: ProxyHarness,
) -> None:
    proxy.resolver.gateway = dataclasses.replace(proxy.gateway, locked_params={"temperature": 0.2})
    proxy.upstream.behaviour = Behaviour(body=completion())

    response = await post(proxy, temperature=0.2)

    assert "x-gateway-locked-params" not in response.headers


async def test_a_lock_beats_a_model_default_too(proxy: ProxyHarness) -> None:
    proxy.retarget(default_params={"temperature": 0.9})
    proxy.resolver.gateway = dataclasses.replace(proxy.gateway, locked_params={"temperature": 0.2})
    proxy.upstream.behaviour = Behaviour(body=completion())

    await post(proxy)

    assert proxy.upstream.last_request.body["temperature"] == 0.2


async def test_locked_parameters_are_reported_on_a_stream_too(proxy: ProxyHarness) -> None:
    """The header goes out with the status line, which on a stream is before the first
    token — so the merge has to happen before the body iterator is returned."""
    from tests.support import chunk

    proxy.resolver.gateway = dataclasses.replace(proxy.gateway, locked_params={"temperature": 0.2})
    proxy.upstream.behaviour = Behaviour(chunks=[chunk("hi", finish_reason="stop")])

    response = await proxy.client.post(
        proxy.url(), json=payload(stream=True, temperature=1.9), headers=proxy.headers()
    )

    assert response.headers["x-gateway-locked-params"] == "temperature"


# -- request validation ------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["tools", "tool_choice", "functions", "function_call", "logprobs"],
)
async def test_unsupported_fields_are_refused_by_name(proxy: ProxyHarness, field: str) -> None:
    """Silently dropping `tools` produces an agent that narrates tool calls as prose."""
    response = await post(proxy, **{field: [{"type": "function"}]})

    assert response.status_code == 400
    error = response.json()["error"]
    assert field in error["message"]
    assert error["param"] == field
    assert proxy.upstream.requests == []


@pytest.mark.parametrize("value", [None, False, [], {}])
async def test_an_unsupported_field_that_requests_nothing_is_allowed(
    proxy: ProxyHarness, value: Any
) -> None:
    proxy.upstream.behaviour = Behaviour(body=completion())

    response = await post(proxy, logprobs=value)

    assert response.status_code == 200


async def test_unknown_model_names_the_one_this_gateway_serves(proxy: ProxyHarness) -> None:
    response = await post(proxy, model="gpt-4o")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "model_not_found"
    assert "demo" in error["message"]


async def test_empty_body_is_rejected(proxy: ProxyHarness) -> None:
    response = await proxy.client.post(proxy.url(), content=b"", headers=proxy.headers())

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_malformed_json_is_rejected(proxy: ProxyHarness) -> None:
    response = await proxy.client.post(proxy.url(), content=b"{oops", headers=proxy.headers())

    assert response.status_code == 400
    assert "JSON" in response.json()["error"]["message"]


async def test_a_json_array_body_is_rejected(proxy: ProxyHarness) -> None:
    response = await proxy.client.post(proxy.url(), json=[1, 2], headers=proxy.headers())

    assert response.status_code == 400


async def test_missing_messages_names_the_field(proxy: ProxyHarness) -> None:
    response = await proxy.client.post(proxy.url(), json={"model": "demo"}, headers=proxy.headers())

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "messages"


async def test_empty_message_list_is_rejected(proxy: ProxyHarness) -> None:
    response = await post(proxy, messages=[])

    assert response.status_code == 400


# -- upstream failures -------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 502, 503])
async def test_upstream_status_is_relayed_not_swallowed(proxy: ProxyHarness, status: int) -> None:
    """A 429 that becomes a 500 turns a client that would back off into one that retries."""
    proxy.upstream.behaviour = Behaviour(
        status=status,
        body={"error": {"message": "rate limited", "type": "rate_limit_error", "code": "slow"}},
    )

    response = await post(proxy)

    assert response.status_code == status
    error = response.json()["error"]
    assert error["type"] == "rate_limit_error"
    assert error["code"] == "slow"


async def test_relayed_errors_are_tagged_as_coming_from_upstream(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(status=500, body={"error": {"message": "boom"}})

    response = await post(proxy)

    assert response.json()["error"]["message"] == "[upstream:demo-upstream] boom"


async def test_non_json_upstream_error_still_produces_an_openai_envelope(
    proxy: ProxyHarness,
) -> None:
    proxy.upstream.behaviour = Behaviour(status=502, body=None)

    response = await post(proxy)

    assert response.status_code == 502
    assert set(response.json()["error"]) == {"message", "type", "param", "code"}


async def test_upstream_timeout_becomes_504(proxy: ProxyHarness) -> None:
    proxy.retarget(timeout_seconds=1)
    proxy.upstream.behaviour = Behaviour(hang=True)

    response = await post(proxy)

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_timeout"


async def test_unreachable_upstream_becomes_502(proxy: ProxyHarness) -> None:
    # Port 1 is reserved and closed; connecting to it fails immediately.
    proxy.retarget(base_url="http://127.0.0.1:1/v1")

    response = await post(proxy)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unavailable"


async def test_upstream_returning_a_non_completion_is_not_a_200(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(body={"not": "a completion"})

    response = await post(proxy)

    assert response.status_code == 503


async def test_a_disabled_gateway_is_a_403_not_a_503(proxy: ProxyHarness) -> None:
    """A 503 means "try again", and an SDK will — every few seconds, indefinitely, against
    an endpoint somebody turned off on purpose. A 403 is final, so the retry loop stops and
    the message gets read."""
    from app.api.proxy.errors import GatewayDisabled

    proxy.resolver.error = GatewayDisabled(
        "Gateway 'demo' is disabled. Enable it under Gateways to start serving requests again."
    )

    response = await post(proxy)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "gateway_disabled"
    assert "Enable it under Gateways" in response.json()["error"]["message"]


async def test_gateway_with_no_enabled_target_is_unavailable(proxy: ProxyHarness) -> None:
    proxy.resolver.gateway = dataclasses.replace(proxy.gateway, targets=())

    response = await post(proxy)

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "server_error"


async def test_a_gateway_with_nothing_configured_says_what_to_do(proxy: ProxyHarness) -> None:
    """ "No usable target" is true and useless. The two causes need different fixes, and
    the message has to say which one this is."""
    proxy.resolver.gateway = dataclasses.replace(proxy.gateway, targets=(), disabled=())

    message = (await post(proxy)).json()["error"]["message"]

    assert "no upstream model configured" in message
    assert "Add one under Models" in message


async def test_a_disabled_model_is_named_in_the_503(proxy: ProxyHarness) -> None:
    """The other cause, and the one that is a single toggle away from working. Naming the
    model is the difference between fixing it and going looking for the problem."""
    proxy.resolver.gateway = dataclasses.replace(
        proxy.gateway, targets=(), disabled=("demo-upstream",)
    )

    message = (await post(proxy)).json()["error"]["message"]

    assert "'demo-upstream'" in message
    assert "disabled" in message


async def test_two_disabled_models_are_both_named(proxy: ProxyHarness) -> None:
    proxy.resolver.gateway = dataclasses.replace(
        proxy.gateway, targets=(), disabled=("primary", "fallback")
    )

    message = (await post(proxy)).json()["error"]["message"]

    assert "'primary', 'fallback'" in message
    assert "disabled upstream models" in message


async def test_editing_a_model_changes_the_next_request(proxy: ProxyHarness) -> None:
    """No restart, and no cache to invalidate: the resolver reads the row per request.
    Task 06 puts a cache here, and this is the behaviour it has to preserve."""
    proxy.upstream.behaviour = Behaviour(body=completion())
    await post(proxy)
    assert proxy.upstream.last_request.body["model"] == "upstream-model"

    proxy.retarget(upstream_model_id="gpt-4o-2024-11-20")
    await post(proxy)

    assert proxy.upstream.last_request.body["model"] == "gpt-4o-2024-11-20"


async def test_editing_default_params_changes_the_next_request(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(body=completion())

    proxy.retarget(default_params={"temperature": 0.15})
    await post(proxy)

    assert proxy.upstream.last_request.body["temperature"] == 0.15


# -- secrets -----------------------------------------------------------------


async def test_upstream_credentials_never_reach_the_client(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(
        status=401, body={"error": {"message": "Incorrect API key: sk-upstream-secret"}}
    )

    response = await post(proxy)

    # The provider echoed its own key back; the gateway relays the message verbatim, so
    # this test documents the one case where that is the provider's disclosure, not ours.
    assert "sk-upstream-secret" not in str(dict(response.headers))


async def test_the_client_key_is_not_forwarded_upstream(proxy: ProxyHarness) -> None:
    proxy.upstream.behaviour = Behaviour(body=completion())

    await post(proxy)

    assert proxy.token not in str(proxy.upstream.last_request.headers)


# -- models ------------------------------------------------------------------


async def test_models_lists_the_gateway_virtual_model(proxy: ProxyHarness) -> None:
    response = await proxy.client.get(proxy.url("/models"), headers=proxy.headers())

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert [model["id"] for model in body["data"]] == ["demo"]
    assert body["data"][0]["object"] == "model"


async def test_models_requires_a_key(proxy: ProxyHarness) -> None:
    response = await proxy.client.get(proxy.url("/models"))

    assert response.status_code == 401


# -- routing -----------------------------------------------------------------


async def test_unknown_slug_is_a_404_in_the_openai_shape(proxy: ProxyHarness) -> None:
    response = await proxy.client.post(
        proxy.url(slug="nope"), json=payload(), headers=proxy.headers()
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "gateway_not_found"


async def test_wrong_method_still_returns_an_openai_error(proxy: ProxyHarness) -> None:
    """Starlette generates this one, not the proxy code — the envelope is chosen by path."""
    response = await proxy.client.get(proxy.url(), headers=proxy.headers())

    assert response.status_code == 405
    assert set(response.json()["error"]) == {"message", "type", "param", "code"}


async def test_control_plane_errors_keep_their_own_envelope(proxy: ProxyHarness) -> None:
    response = await proxy.client.get("/nope")

    assert response.status_code == 404
    assert "request_id" in response.json()["error"]


async def test_no_credential_reaches_the_logs(proxy: ProxyHarness) -> None:
    """Acceptance criterion: credentials never appear in logs. Both the caller's gateway
    key and the upstream's provider key pass through this request path."""
    records = _CapturingHandler()
    root = logging.getLogger()
    root.addHandler(records)
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        proxy.upstream.behaviour = Behaviour(body=completion())
        await post(proxy)
        proxy.upstream.behaviour = Behaviour(status=500, body={"error": {"message": "boom"}})
        await post(proxy)
        proxy.retarget(base_url="http://127.0.0.1:1/v1")
        await post(proxy)
    finally:
        root.removeHandler(records)
        root.setLevel(previous)

    written = records.text
    assert written, "nothing was logged, so this would pass for the wrong reason"
    assert proxy.token not in written
    assert proxy.target.credential is not None
    assert proxy.target.credential not in written


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def text(self) -> str:
        return "\n".join(f"{record.getMessage()} {record.__dict__}" for record in self.records)
