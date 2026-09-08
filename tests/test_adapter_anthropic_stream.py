"""Anthropic's event stream, translated — driven from recorded fixtures.

The fixtures are files rather than lists built here, for the reason
``tests/fixtures/anthropic/README.md`` gives at length: an event list assembled next to
the translator drifts toward whatever the translator already expects, and every bug worth
catching lives in the gap between that and what the provider sends.

What is asserted is what a *client* would have seen — the deltas, the finish reason, the
usage — rather than the translator's internals. A test that asserted the internals would
pass while the SDK on the other end read nothing.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.adapters.anthropic import AnthropicAdapter
from app.adapters.base import UpstreamStreamFailed
from app.schemas.openai import ChatChunk, ChatRequest, StreamFrame

adapter = AnthropicAdapter()

FIXTURES = Path(__file__).parent / "fixtures" / "anthropic"


def request(**overrides: Any) -> ChatRequest:
    values: dict[str, Any] = {
        "model": "demo",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    values.update(overrides)
    return ChatRequest.model_validate(values)


class _AsyncBytes(httpx.AsyncByteStream):
    """One fixture, delivered in packets that do not line up with event boundaries.

    The split is at a fixed size rather than at ``\\n\\n`` on purpose: a translator that
    assumed one read is one event would pass every test written the other way and drop
    tokens in production.
    """

    def __init__(self, payload: bytes, *, packet: int = 37) -> None:
        self._payload = payload
        self._packet = packet

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self._payload), self._packet):
            yield self._payload[start : start + self._packet]


async def translate(name: str, **overrides: Any) -> list[StreamFrame]:
    payload = (FIXTURES / f"{name}.sse").read_bytes()
    response = httpx.Response(200, stream=_AsyncBytes(payload))
    return [frame async for frame in adapter.parse_stream(response, request(**overrides))]


def chunks(frames: list[StreamFrame]) -> list[dict[str, Any]]:
    """The frames as the client parses them — from ``data``, not from ``chunk``."""
    return [json.loads(frame.data) for frame in frames]


def deltas(frames: list[StreamFrame]) -> list[str]:
    return [
        choice["delta"]["content"]
        for payload in chunks(frames)
        for choice in payload["choices"]
        if choice["delta"].get("content")
    ]


def finishes(frames: list[StreamFrame]) -> list[str]:
    return [
        choice["finish_reason"]
        for payload in chunks(frames)
        for choice in payload["choices"]
        if choice.get("finish_reason")
    ]


# ---------------------------------------------------------------------------
# the ordinary stream
# ---------------------------------------------------------------------------


async def test_the_completion_arrives_delta_by_delta() -> None:
    assert deltas(await translate("normal")) == ["Hello", ", world", "!"]


async def test_the_first_chunk_announces_the_role() -> None:
    """OpenAI's own first chunk carries the role and no content, and more than one client
    reads it to decide it is looking at an assistant message."""
    first = chunks(await translate("normal"))[0]

    assert first["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert first["choices"][0]["finish_reason"] is None


async def test_the_last_chunk_carries_the_finish_reason() -> None:
    frames = await translate("normal")

    assert finishes(frames) == ["stop"]
    assert chunks(frames)[-1]["choices"][0]["delta"] == {}


async def test_pings_are_invisible_to_the_client() -> None:
    """Two of them, in the middle of the fixture. A relayed keepalive is a frame a client
    has to parse and discard, and some SDKs will not."""
    assert all("ping" not in frame.data for frame in await translate("normal"))


async def test_the_terminator_is_not_the_adapters_to_write() -> None:
    """The gateway writes ``[DONE]`` once the stream really ends — an adapter that wrote
    its own would produce two."""
    assert all("[DONE]" not in frame.data for frame in await translate("normal"))


async def test_every_chunk_is_openai_shaped() -> None:
    for payload in chunks(await translate("normal")):
        assert payload["object"] == "chat.completion.chunk"
        assert payload["id"] == "chatcmpl-msg_01XFDUDYJgAACzvnptvVoYEL"
        assert payload["model"] == "claude-sonnet-4-5-20250929"
        assert isinstance(payload["created"], int)


async def test_the_created_timestamp_is_the_same_on_every_chunk() -> None:
    """One generation is one completion, and a client stitching chunks by id and created
    must not see the second field move underneath it."""
    assert len({payload["created"] for payload in chunks(await translate("normal"))}) == 1


async def test_the_parsed_view_and_the_wire_bytes_cannot_disagree() -> None:
    """The request log reads ``chunk`` while the client reads ``data``. For a translating
    dialect those are two renderings of one object, and this is what keeps them one."""
    for frame in await translate("normal"):
        assert frame.chunk is not None
        assert frame.chunk == ChatChunk.model_validate_json(frame.data)


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------


async def test_usage_is_not_sent_unless_it_was_asked_for() -> None:
    """OpenAI's behaviour, matched: ``include_usage`` is opt-in, and a client that did not
    ask gets the frames it expects."""
    assert all(json.loads(frame.data).get("usage") is None for frame in await translate("normal"))


async def test_usage_arrives_in_its_own_final_chunk_when_asked_for() -> None:
    frames = await translate("normal", stream_options={"include_usage": True})
    last = chunks(frames)[-1]

    assert last["choices"] == []
    assert last["usage"] == {"prompt_tokens": 25, "completion_tokens": 15, "total_tokens": 40}


async def test_the_usage_chunk_comes_after_the_finish_reason() -> None:
    frames = await translate("normal", stream_options={"include_usage": True})
    payloads = chunks(frames)

    assert payloads[-2]["choices"][0]["finish_reason"] == "stop"
    assert payloads[-1]["usage"] is not None


async def test_the_output_count_is_the_cumulative_one_not_the_first_guess() -> None:
    """``message_start`` reports ``output_tokens: 1`` before anything has been generated;
    ``message_delta`` reports the real total. Adding them would overcount by one on every
    request, which is a number task 14 charges against a budget."""
    frames = await translate("normal", stream_options={"include_usage": True})

    assert chunks(frames)[-1]["usage"]["completion_tokens"] == 15


# ---------------------------------------------------------------------------
# the other endings
# ---------------------------------------------------------------------------


async def test_a_generation_cut_short_says_length() -> None:
    """``max_tokens`` is how a truncated completion is distinguished from a finished one,
    and a client that retries on truncation reads exactly this field."""
    assert finishes(await translate("truncated")) == ["length"]


async def test_a_stop_sequence_ending_reads_as_stop() -> None:
    assert finishes(await translate("prefill")) == ["stop"]


async def test_an_error_event_ends_the_stream_as_a_failure() -> None:
    """SPEC §8.2: the status line went out with the first frame, so this cannot become a
    503. The proxy turns it into a terminating error frame and marks the row."""
    with pytest.raises(UpstreamStreamFailed, match="overloaded_error"):
        await translate("overloaded_midstream")


async def test_what_was_generated_before_the_error_still_reached_the_client() -> None:
    frames: list[StreamFrame] = []
    payload = (FIXTURES / "overloaded_midstream.sse").read_bytes()
    response = httpx.Response(200, stream=_AsyncBytes(payload))

    with pytest.raises(UpstreamStreamFailed):
        async for frame in adapter.parse_stream(response, request()):
            frames.append(frame)

    assert deltas(frames) == ["Half a se"]


# ---------------------------------------------------------------------------
# blocks that are not the assistant speaking
# ---------------------------------------------------------------------------


async def test_a_thinking_block_never_reaches_the_client() -> None:
    """A whole content block of reasoning, ahead of the answer. Relaying it as content
    would make the caller read a monologue as the reply."""
    assert deltas(await translate("thinking")) == ["Forty-two."]


async def test_a_prefilled_content_block_is_delivered() -> None:
    """The opposite case, and the reason ``content_block_start`` is not simply ignored:
    text that arrives in the *start* event is text the client asked to continue from."""
    assert deltas(await translate("prefill")) == ['{"answer":', " 42}"]


# ---------------------------------------------------------------------------
# robustness
# ---------------------------------------------------------------------------


async def test_an_unparseable_event_is_skipped_rather_than_relayed() -> None:
    """The opposite of the openai dialect's choice, and deliberately so: there, the
    provider's bytes are the client's bytes. Here a frame the gateway cannot read is one
    it cannot translate, and Anthropic's own wire format would mean nothing downstream."""
    response = httpx.Response(200, stream=_AsyncBytes(b"event: message_stop\ndata: {oops\n\n"))

    assert [frame async for frame in adapter.parse_stream(response, request())] == []


async def test_an_unknown_event_type_is_ignored() -> None:
    """A provider shipping a new event must not break a stream in flight."""
    payload = b'data: {"type":"something_new","surprise":true}\n\n'
    response = httpx.Response(200, stream=_AsyncBytes(payload))

    assert [frame async for frame in adapter.parse_stream(response, request())] == []


async def test_a_stream_that_stops_without_message_stop_yields_what_it_had() -> None:
    """A provider that dies mid-generation without an error event. The frames already
    delivered stand; the proxy's own terminator is what ends it downstream."""
    payload = (
        b'data: {"type":"message_start","message":{"id":"msg_1","model":"m",'
        b'"usage":{"input_tokens":3,"output_tokens":1}}}\n\n'
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"half"}}\n\n'
    )
    response = httpx.Response(200, stream=_AsyncBytes(payload))

    frames = [frame async for frame in adapter.parse_stream(response, request())]

    assert deltas(frames) == ["half"]
    assert finishes(frames) == []
