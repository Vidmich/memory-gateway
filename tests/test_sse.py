"""SSE parsing.

Every case here is one that a provider actually produces: keepalive comments, CRLF line
endings, multi-line data, and — most importantly — events arriving split across reads in
ways unrelated to frame boundaries.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.services.sse import MAX_FRAME_BYTES, MalformedStream, format_event, iter_sse


async def _feed(*packets: bytes) -> AsyncIterator[bytes]:
    for packet in packets:
        yield packet


async def _collect(*packets: bytes) -> list[str]:
    return [event.data async for event in iter_sse(_feed(*packets))]


async def test_simple_frames() -> None:
    assert await _collect(b'data: {"a":1}\n\n', b"data: [DONE]\n\n") == ['{"a":1}', "[DONE]"]


async def test_two_events_in_one_packet() -> None:
    assert await _collect(b"data: one\n\ndata: two\n\n") == ["one", "two"]


async def test_one_event_split_across_packets() -> None:
    assert await _collect(b"data: hel", b"lo wor", b"ld\n", b"\n") == ["hello world"]


async def test_split_inside_the_field_name() -> None:
    assert await _collect(b"da", b"ta: value\n\n") == ["value"]


async def test_crlf_line_endings() -> None:
    assert await _collect(b"data: value\r\n\r\n") == ["value"]


async def test_crlf_split_between_packets() -> None:
    """A packet ending in a bare CR must not be treated as a line ending: the LF that
    completes it arrives next."""
    assert await _collect(b"data: value\r", b"\n\r\n") == ["value"]


async def test_lone_cr_line_endings() -> None:
    assert await _collect(b"data: value\r\r") == ["value"]


async def test_multi_line_data_is_joined_with_newlines() -> None:
    assert await _collect(b"data: line one\ndata: line two\n\n") == ["line one\nline two"]


async def test_comments_and_keepalives_are_ignored() -> None:
    assert await _collect(b": ping\n\ndata: real\n\n") == ["real"]


async def test_leading_space_after_the_colon_is_stripped_once() -> None:
    assert await _collect(b"data:  two spaces\n\n") == [" two spaces"]


async def test_field_without_a_colon_is_tolerated() -> None:
    events = [event async for event in iter_sse(_feed(b"data\n\n"))]

    assert [event.data for event in events] == [""]


async def test_event_and_id_fields_are_captured() -> None:
    events = [event async for event in iter_sse(_feed(b"event: ping\nid: 7\ndata: x\n\n"))]

    assert (events[0].event, events[0].id, events[0].data) == ("ping", "7", "x")


async def test_unknown_fields_are_skipped() -> None:
    assert await _collect(b"retry: 500\ndata: x\n\n") == ["x"]


async def test_trailing_frame_without_a_blank_line_is_still_delivered() -> None:
    """Dropping the last token because a provider omitted the final newline would be a
    silent truncation."""
    assert await _collect(b"data: last\n") == ["last"]


async def test_empty_stream_yields_nothing() -> None:
    assert await _collect() == []


async def test_trailing_blank_lines_do_not_emit_empty_events() -> None:
    assert await _collect(b"data: x\n\n\n\n") == ["x"]


async def test_a_frame_that_never_ends_is_refused() -> None:
    with pytest.raises(MalformedStream):
        await _collect(b"data: " + b"x" * (MAX_FRAME_BYTES + 1))


async def test_invalid_utf8_does_not_kill_the_stream() -> None:
    assert await _collect(b"data: \xff\xfe\n\n") == ["��"]


def test_format_event_writes_one_data_line_per_line() -> None:
    assert format_event("a\nb") == "data: a\ndata: b\n\n"


def test_format_event_round_trips_through_the_parser() -> None:
    payload = '{"choices":[{"delta":{"content":"hi"}}]}'

    assert format_event(payload) == f"data: {payload}\n\n"
