"""One table of cases, run against every dialect, asserting identical OpenAI-shaped output.

This is the deliverable of task 16 rather than a check on it. The gateway's whole premise
is that the client does not change, and the way that premise dies is not in a single wrong
translation — it is in drift: OpenAI ships a field, somebody adds it to one adapter, and
six months later two upstreams behind the same gateway answer the same question in two
subtly different shapes. A table both adapters have to satisfy is what makes that a
failing build instead of a support ticket.

The structure is deliberate. A :class:`Generation` describes what happened in *neither*
provider's terms — this much text, arriving in these pieces, ending this way, costing
these tokens. Each :class:`Dialect` knows how to render that as its own provider's
response, and the assertions run the result through that dialect's adapter and compare the
normalised OpenAI view. Adding a dialect means adding one :class:`Dialect` here, and every
case runs against it.

What is deliberately *not* compared is the model name and the numeric part of the response
id: those are the provider's own, and a contract that demanded they match would be
demanding a lie.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from app.adapters.anthropic import AnthropicAdapter
from app.adapters.base import UpstreamAdapter, UpstreamError, UpstreamTarget
from app.adapters.openai import OpenAIAdapter
from app.api.proxy.errors import UpstreamStatus
from app.core.ids import uuid7
from app.schemas.openai import ChatChunk, ChatRequest, ChatResponse
from app.services.routing import is_retryable

# ---------------------------------------------------------------------------
# what happened, in neither provider's words
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Generation:
    """One completion, described once."""

    name: str
    deltas: tuple[str, ...]
    #: The ``finish_reason`` a client must see, whichever upstream served it.
    finish: str
    #: How Anthropic says the same thing.
    anthropic_stop: str
    prompt_tokens: int
    completion_tokens: int

    @property
    def text(self) -> str:
        return "".join(self.deltas)


GENERATIONS = (
    Generation(
        name="an ordinary answer",
        deltas=("Hello", ", world", "!"),
        finish="stop",
        anthropic_stop="end_turn",
        prompt_tokens=25,
        completion_tokens=15,
    ),
    Generation(
        name="cut short by the token limit",
        deltas=("One two three four",),
        finish="length",
        anthropic_stop="max_tokens",
        prompt_tokens=11,
        completion_tokens=4,
    ),
    Generation(
        name="ended by a stop sequence",
        deltas=('{"answer":', " 42}"),
        finish="stop",
        anthropic_stop="stop_sequence",
        prompt_tokens=19,
        completion_tokens=6,
    ),
    Generation(
        name="a model with nothing to say",
        deltas=(),
        finish="stop",
        anthropic_stop="end_turn",
        prompt_tokens=8,
        completion_tokens=0,
    ),
    Generation(
        name="text a naive translator would mangle",
        deltas=('He said "hi"\n', "— and\ttabbed", " 🎉"),
        finish="stop",
        anthropic_stop="end_turn",
        prompt_tokens=30,
        completion_tokens=9,
    ),
)


@dataclass(frozen=True, slots=True)
class Failure:
    """One way a provider says no — said twice, because the two say it differently.

    Both a status *and* a type per provider, because the wire forms genuinely diverge:
    "I am overloaded, try again shortly" is a 503 from an OpenAI-shaped service and a 529
    from Anthropic. What must not diverge is ``client_status`` — the one the caller's SDK
    keys its exception class and its backoff on.
    """

    name: str
    #: The status the client must end up with, which is not always either provider's.
    client_status: int
    openai_status: int
    openai_type: str
    anthropic_status: int
    anthropic_type: str
    message: str


FAILURES = (
    Failure(
        name="a bad request",
        client_status=400,
        openai_status=400,
        openai_type="invalid_request_error",
        anthropic_status=400,
        anthropic_type="invalid_request_error",
        message="max_tokens: must be greater than 0",
    ),
    Failure(
        name="a rejected key",
        client_status=401,
        openai_status=401,
        openai_type="authentication_error",
        anthropic_status=401,
        anthropic_type="authentication_error",
        message="invalid x-api-key",
    ),
    Failure(
        name="rate limited",
        client_status=429,
        openai_status=429,
        openai_type="rate_limit_error",
        anthropic_status=429,
        anthropic_type="rate_limit_error",
        message="Number of requests has exceeded your rate limit",
    ),
    Failure(
        name="a provider fault",
        client_status=500,
        openai_status=500,
        openai_type="api_error",
        anthropic_status=500,
        anthropic_type="api_error",
        message="Internal server error",
    ),
    Failure(
        # The one the whole error half of this file exists for: the two providers say the
        # same thing with different numbers, and SPEC §8.2's retry table has never heard
        # of a 529 — so an untranslated one is treated as final and the failover chain
        # stops on the one failure retrying was invented for.
        name="overloaded",
        client_status=503,
        openai_status=503,
        openai_type="api_error",
        anthropic_status=529,
        anthropic_type="overloaded_error",
        message="Overloaded",
    ),
)


# ---------------------------------------------------------------------------
# how each provider says it
# ---------------------------------------------------------------------------


def openai_completion(case: Generation) -> dict[str, Any]:
    return {
        "id": "chatcmpl-7QyqpwdfhqwajicIEznoc6Q47XAyW",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": case.text},
                "finish_reason": case.finish,
            }
        ],
        "usage": {
            "prompt_tokens": case.prompt_tokens,
            "completion_tokens": case.completion_tokens,
            "total_tokens": case.prompt_tokens + case.completion_tokens,
        },
    }


def anthropic_completion(case: Generation) -> dict[str, Any]:
    return {
        "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5-20250929",
        "content": [{"type": "text", "text": part} for part in case.deltas],
        "stop_reason": case.anthropic_stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": case.prompt_tokens,
            "output_tokens": case.completion_tokens,
        },
    }


def openai_stream(case: Generation, *, include_usage: bool) -> bytes:
    """What an OpenAI-shaped provider puts on the wire, frame for frame."""
    head = {
        "id": "chatcmpl-7QyqpwdfhqwajicIEznoc6Q47XAyW",
        "object": "chat.completion.chunk",
        "created": 1_700_000_000,
        "model": "gpt-4o-mini",
    }
    frames: list[dict[str, Any]] = [
        {**head, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}
    ]
    frames += [
        {**head, "choices": [{"index": 0, "delta": {"content": part}}]} for part in case.deltas
    ]
    frames.append({**head, "choices": [{"index": 0, "delta": {}, "finish_reason": case.finish}]})
    if include_usage:
        frames.append(
            {
                **head,
                "choices": [],
                "usage": {
                    "prompt_tokens": case.prompt_tokens,
                    "completion_tokens": case.completion_tokens,
                    "total_tokens": case.prompt_tokens + case.completion_tokens,
                },
            }
        )
    body = b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames)
    return body + b"data: [DONE]\n\n"


def anthropic_stream(case: Generation, *, include_usage: bool) -> bytes:
    """The same generation as Anthropic's event sequence, pings and all.

    ``include_usage`` is ignored: Anthropic always reports usage and the *adapter* decides
    whether the client sees it, which is half of what these tests are checking.
    """
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5-20250929",
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": case.prompt_tokens, "output_tokens": 1},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "ping"},
    ]
    events += [
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": part},
        }
        for part in case.deltas
    ]
    events += [
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": case.anthropic_stop, "stop_sequence": None},
            "usage": {"output_tokens": case.completion_tokens},
        },
        {"type": "message_stop"},
    ]
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events
    )


def openai_error(failure: Failure) -> tuple[int, dict[str, Any]]:
    return failure.openai_status, {
        "error": {
            "message": failure.message,
            "type": failure.openai_type,
            "code": failure.anthropic_type,
            "param": None,
        }
    }


def anthropic_error(failure: Failure) -> tuple[int, dict[str, Any]]:
    return failure.anthropic_status, {
        "type": "error",
        "error": {"type": failure.anthropic_type, "message": failure.message},
    }


@dataclass(frozen=True, slots=True)
class Dialect:
    name: str
    adapter: UpstreamAdapter
    completion: Callable[[Generation], dict[str, Any]]
    stream: Callable[..., bytes]
    error: Callable[[Failure], tuple[int, dict[str, Any]]]

    def target(self) -> UpstreamTarget:
        return UpstreamTarget(
            id=uuid7(),
            name=f"{self.name}-upstream",
            base_url="https://provider.test/v1",
            dialect=self.name,
            upstream_model_id="the-model",
            auth_type="bearer",
            credential="sk-secret",
        )


DIALECTS = (
    Dialect("openai", OpenAIAdapter(), openai_completion, openai_stream, openai_error),
    Dialect(
        "anthropic",
        AnthropicAdapter(),
        anthropic_completion,
        anthropic_stream,
        anthropic_error,
    ),
)


def ids(items: tuple[Any, ...]) -> list[str]:
    return [item.name for item in items]


# ---------------------------------------------------------------------------
# the normalised view both must produce
# ---------------------------------------------------------------------------


def completion_view(response: ChatResponse) -> dict[str, Any]:
    """A completion as a client reads it, with the provider's own names left out."""
    choice = response.choices[0]
    assert choice.message is not None
    assert response.usage is not None
    return {
        "object": response.object,
        "role": choice.message.role,
        "content": choice.message.content,
        "finish_reason": choice.finish_reason,
        "usage": {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
        },
    }


def stream_view(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "object": sorted({payload["object"] for payload in payloads}),
        "role_announced": payloads[0]["choices"][0]["delta"].get("role"),
        "content": "".join(
            choice["delta"].get("content") or ""
            for payload in payloads
            for choice in payload["choices"]
        ),
        "finish_reasons": [
            choice["finish_reason"]
            for payload in payloads
            for choice in payload["choices"]
            if choice.get("finish_reason")
        ],
        "usage": next(
            (payload["usage"] for payload in reversed(payloads) if payload.get("usage")), None
        ),
    }


def error_view(failure: UpstreamError) -> dict[str, Any]:
    return {
        "status_code": failure.status_code,
        "type": failure.type,
        "code": failure.code,
        "message": failure.message,
    }


class _AsyncBytes(httpx.AsyncByteStream):
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self) -> AsyncIterator[bytes]:
        # Split at a size unrelated to the frame boundaries, because a provider's packets
        # are unrelated to them too.
        for start in range(0, len(self._payload), 41):
            yield self._payload[start : start + 41]


async def stream_payloads(
    dialect: Dialect, case: Generation, *, include_usage: bool
) -> list[dict[str, Any]]:
    response = httpx.Response(
        200, stream=_AsyncBytes(dialect.stream(case, include_usage=include_usage))
    )
    request = ChatRequest.model_validate(
        {
            "model": "demo",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            **({"stream_options": {"include_usage": True}} if include_usage else {}),
        }
    )
    return [
        json.loads(frame.data) async for frame in dialect.adapter.parse_stream(response, request)
    ]


# ---------------------------------------------------------------------------
# the contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", GENERATIONS, ids=ids(GENERATIONS))
def test_every_dialect_produces_the_same_completion(case: Generation) -> None:
    views = {
        dialect.name: completion_view(
            dialect.adapter.parse(httpx.Response(200, json=dialect.completion(case)))
        )
        for dialect in DIALECTS
    }

    assert len(set(map(json.dumps, views.values()))) == 1, views


@pytest.mark.parametrize("case", GENERATIONS, ids=ids(GENERATIONS))
def test_every_completion_id_is_one_a_client_recognises(case: Generation) -> None:
    for dialect in DIALECTS:
        parsed = dialect.adapter.parse(httpx.Response(200, json=dialect.completion(case)))
        assert parsed.id.startswith("chatcmpl-"), dialect.name


@pytest.mark.parametrize("case", GENERATIONS, ids=ids(GENERATIONS))
@pytest.mark.parametrize("include_usage", [False, True], ids=["plain", "with usage"])
async def test_every_dialect_produces_the_same_stream(
    case: Generation, include_usage: bool
) -> None:
    views = {}
    for dialect in DIALECTS:
        payloads = await stream_payloads(dialect, case, include_usage=include_usage)
        views[dialect.name] = stream_view(payloads)

    assert len(set(map(json.dumps, views.values()))) == 1, views


@pytest.mark.parametrize("case", GENERATIONS, ids=ids(GENERATIONS))
async def test_a_stream_reassembles_into_the_completion_it_would_have_returned(
    case: Generation,
) -> None:
    """Streamed and non-streamed have to be two renderings of one answer.

    Task 07 stores exactly this reassembly as the transcript for a streamed request, and a
    dialect where the two disagree makes every stored streamed body subtly wrong.
    """
    for dialect in DIALECTS:
        payloads = await stream_payloads(dialect, case, include_usage=True)
        whole = dialect.adapter.parse(httpx.Response(200, json=dialect.completion(case)))

        streamed = stream_view(payloads)
        assert streamed["content"] == whole.choices[0].message.content, dialect.name  # type: ignore[union-attr]
        assert streamed["finish_reasons"] == [whole.choices[0].finish_reason], dialect.name
        assert whole.usage is not None
        assert streamed["usage"] == {
            "prompt_tokens": whole.usage.prompt_tokens,
            "completion_tokens": whole.usage.completion_tokens,
            "total_tokens": whole.usage.total_tokens,
        }, dialect.name


@pytest.mark.parametrize("case", GENERATIONS, ids=ids(GENERATIONS))
async def test_every_frame_parses_as_a_completion_chunk(case: Generation) -> None:
    """The gateway's own view of a frame is what the request log's token counts and the
    stream tee are built on, so a frame it cannot parse is a row with holes in it."""
    for dialect in DIALECTS:
        for payload in await stream_payloads(dialect, case, include_usage=True):
            ChatChunk.model_validate(payload)


@pytest.mark.parametrize("failure", FAILURES, ids=ids(FAILURES))
def test_every_dialect_reports_the_same_failure(failure: Failure) -> None:
    views = {}
    for dialect in DIALECTS:
        status, body = dialect.error(failure)
        views[dialect.name] = error_view(dialect.adapter.error(httpx.Response(status, json=body)))

    assert len(set(map(json.dumps, views.values()))) == 1, views
    assert next(iter(views.values()))["status_code"] == failure.client_status


@pytest.mark.parametrize("failure", FAILURES, ids=ids(FAILURES))
def test_every_dialect_agrees_on_whether_to_try_the_next_target(failure: Failure) -> None:
    """SPEC §8.2's decision, which is the one a status remap exists to get right: a
    provider saying "overloaded, come back" must move a failover chain along, and it only
    does if the translation lands on a status the table knows."""
    decisions = set()
    for dialect in DIALECTS:
        status, body = dialect.error(failure)
        translated = dialect.adapter.error(httpx.Response(status, json=body))
        decisions.add(
            is_retryable(
                UpstreamStatus(
                    status_code=translated.status_code,
                    model_name=dialect.name,
                    message=translated.message,
                    upstream_type=translated.type,
                    upstream_code=translated.code,
                )
            )
        )

    assert len(decisions) == 1, f"{failure.name}: the dialects disagree"


def test_the_table_covers_every_registered_dialect() -> None:
    """The guard that keeps this file honest. A dialect added to the registry and not to
    the table above would leave every assertion here passing while testing nothing about
    it — which is precisely the drift this file exists to prevent.
    """
    from app.adapters import known_dialects

    assert set(known_dialects()) == {dialect.name for dialect in DIALECTS}


@pytest.mark.parametrize("dialect", DIALECTS, ids=ids(DIALECTS))
def test_every_dialect_sends_the_credential_and_a_timeout(dialect: Dialect) -> None:
    """Not a translation question, but a contract nonetheless: whatever the dialect, a
    configured credential goes out and the model's timeout rides on the request."""
    request = ChatRequest.model_validate(
        {"model": "demo", "messages": [{"role": "user", "content": "hi"}]}
    )
    prepared = dialect.adapter.prepare(request, dialect.target())

    assert prepared.headers["authorization"] == "Bearer sk-secret"
    assert prepared.extensions["timeout"]["read"] == 60
    assert prepared.method == "POST"
