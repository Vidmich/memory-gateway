"""The openai dialect adapter: request preparation, auth styles, and stream parsing."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.adapters.base import UpstreamTarget, get_adapter, known_dialects
from app.adapters.openai import OpenAIAdapter, chat_completions_url
from app.core.ids import uuid7
from app.schemas.openai import ChatRequest

adapter = OpenAIAdapter()


def target(**overrides: Any) -> UpstreamTarget:
    values: dict[str, Any] = {
        "id": uuid7(),
        "name": "provider",
        "base_url": "https://api.example.com/v1",
        "dialect": "openai",
        "upstream_model_id": "gpt-4o-mini",
        "auth_type": "bearer",
        "credential": "sk-secret",
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return UpstreamTarget(**values)


def request(**overrides: Any) -> ChatRequest:
    values: dict[str, Any] = {
        "model": "demo",
        "messages": [{"role": "user", "content": "hi"}],
    }
    values.update(overrides)
    return ChatRequest.model_validate(values)


def body_of(prepared: httpx.Request) -> dict[str, Any]:
    return json.loads(prepared.content)  # type: ignore[no-any-return]


# -- auth styles -------------------------------------------------------------


def test_bearer_auth() -> None:
    prepared = adapter.prepare(request(), target())

    assert prepared.headers["authorization"] == "Bearer sk-secret"


def test_api_key_header_auth() -> None:
    prepared = adapter.prepare(request(), target(auth_type="api_key_header"))

    assert prepared.headers["x-api-key"] == "sk-secret"
    assert "authorization" not in prepared.headers


def test_azure_auth() -> None:
    prepared = adapter.prepare(request(), target(auth_type="azure"))

    assert prepared.headers["api-key"] == "sk-secret"


def test_no_auth_sends_no_credential_header() -> None:
    prepared = adapter.prepare(request(), target(auth_type="none", credential=None))

    assert "authorization" not in prepared.headers
    assert "x-api-key" not in prepared.headers


def test_missing_credential_is_treated_as_no_auth() -> None:
    """A model configured for bearer auth with no stored credential must not send the
    literal string ``Bearer None``."""
    prepared = adapter.prepare(request(), target(credential=None))

    assert "authorization" not in prepared.headers


def test_unknown_auth_type_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="auth_type"):
        adapter.prepare(request(), target(auth_type="oauth2"))


def test_extra_headers_are_applied_and_win() -> None:
    prepared = adapter.prepare(
        request(),
        target(extra_headers={"X-Org": "acme", "Authorization": "Bearer override"}),
    )

    assert prepared.headers["x-org"] == "acme"
    assert prepared.headers["authorization"] == "Bearer override"


# -- URLs --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.example.com/v1", "https://api.example.com/v1/chat/completions"),
        ("https://api.example.com/v1/", "https://api.example.com/v1/chat/completions"),
        ("https://api.example.com", "https://api.example.com/chat/completions"),
        (
            "https://x.openai.azure.com/openai/deployments/d?api-version=2024-06-01",
            "https://x.openai.azure.com/openai/deployments/d/chat/completions"
            "?api-version=2024-06-01",
        ),
    ],
)
def test_endpoint_url(base_url: str, expected: str) -> None:
    assert chat_completions_url(base_url) == expected


# -- body --------------------------------------------------------------------


def test_virtual_model_is_replaced_by_the_upstream_id() -> None:
    body = body_of(adapter.prepare(request(model="demo"), target()))

    assert body["model"] == "gpt-4o-mini"


def test_unknown_client_fields_are_forwarded() -> None:
    """The gateway is a proxy; a field it has never heard of belongs to the provider."""
    body = body_of(adapter.prepare(request(reasoning_effort="high"), target()))

    assert body["reasoning_effort"] == "high"


def test_unsupported_fields_never_reach_the_upstream() -> None:
    prepared = adapter.prepare(request(tools=[{"type": "function"}], logprobs=True), target())

    assert "tools" not in body_of(prepared)
    assert "logprobs" not in body_of(prepared)


def test_unset_parameters_are_not_sent_as_null() -> None:
    body = body_of(adapter.prepare(request(), target()))

    assert "temperature" not in body
    assert "stop" not in body


def test_streaming_flag_and_accept_header() -> None:
    prepared = adapter.prepare(request(stream=True), target())

    assert body_of(prepared)["stream"] is True
    assert prepared.headers["accept"] == "text/event-stream"


def test_per_model_timeout_rides_on_the_request() -> None:
    prepared = adapter.prepare(request(), target(timeout_seconds=7))

    assert prepared.extensions["timeout"]["read"] == 7


# -- responses ---------------------------------------------------------------


def test_parse_reads_a_completion() -> None:
    payload = {
        "id": "chatcmpl-1",
        "model": "gpt-4o-mini",
        "created": 1,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
    }
    parsed = adapter.parse(httpx.Response(200, json=payload))

    assert parsed.choices[0].message is not None
    assert parsed.choices[0].message.content == "hi"


def test_parse_rejects_a_body_that_is_not_a_completion() -> None:
    from app.adapters.base import MalformedUpstreamResponse

    with pytest.raises(MalformedUpstreamResponse):
        adapter.parse(httpx.Response(200, text="<html>gateway timeout</html>"))


async def _frames(payload: bytes) -> list[tuple[str, bool]]:
    async def stream() -> AsyncIterator[bytes]:
        yield payload

    response = httpx.Response(200, stream=_AsyncBytes(stream()))
    return [
        (frame.data, frame.chunk is not None)
        async for frame in adapter.parse_stream(response, request(stream=True))
    ]


class _AsyncBytes(httpx.AsyncByteStream):
    def __init__(self, source: AsyncIterator[bytes]) -> None:
        self._source = source

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for part in self._source:
            yield part


async def test_stream_frames_are_relayed_verbatim() -> None:
    payload = b'data: {"id":"1","choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'

    frames = await _frames(payload)

    assert frames == [('{"id":"1","choices":[{"delta":{"content":"hi"}}]}', True)]


async def test_malformed_frames_are_still_relayed() -> None:
    """The client's SDK is a better judge of the provider's output than the gateway is;
    only the gateway's parsed view is lost."""
    frames = await _frames(b"data: not-json\n\n")

    assert frames == [("not-json", False)]


async def test_done_is_not_relayed_by_the_adapter() -> None:
    assert await _frames(b"data: [DONE]\n\n") == []


# -- registry ----------------------------------------------------------------


def test_openai_dialect_is_registered() -> None:
    assert get_adapter("openai").dialect == "openai"
    assert "openai" in known_dialects()


def test_unknown_dialect_raises() -> None:
    with pytest.raises(ValueError, match="no adapter"):
        get_adapter("bedrock")


def test_target_repr_hides_the_credential() -> None:
    """A target can end up in a log line or a traceback; the credential must not."""
    assert "sk-secret" not in repr(target())
