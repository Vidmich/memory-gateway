"""Server-sent events parsing.

Written against the byte stream rather than lines because a provider's frames arrive
split across TCP packets in ways that have nothing to do with event boundaries: a single
``data:`` line routinely spans two reads, and two events routinely arrive in one. Anything
that assumes one read is one event will look correct locally and drop tokens in
production.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DONE = "[DONE]"

# A frame this large is a broken or hostile upstream, not a completion. Without a cap the
# parser buffers indefinitely on a stream that never sends a newline.
MAX_FRAME_BYTES = 1024 * 1024


class MalformedStream(Exception):
    """The upstream sent something that cannot be parsed as SSE."""


@dataclass(frozen=True, slots=True)
class SSEEvent:
    data: str
    event: str | None = None
    id: str | None = None


async def iter_sse(stream: AsyncIterator[bytes]) -> AsyncIterator[SSEEvent]:
    """Yield events from a raw byte stream as soon as each one completes."""
    buffer = ""
    data_lines: list[str] = []
    event_name: str | None = None
    event_id: str | None = None
    saw_field = False

    async for raw in stream:
        buffer += raw.decode("utf-8", errors="replace")
        if len(buffer) > MAX_FRAME_BYTES:
            raise MalformedStream(f"no event boundary within {MAX_FRAME_BYTES} bytes")

        # A trailing CR may be the first half of a CRLF still in flight; hold it back
        # rather than treating it as a line ending and splitting a frame in two.
        if buffer.endswith("\r"):
            head, buffer = buffer[:-1], "\r"
        else:
            head, buffer = buffer, ""

        lines = head.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        # The final element is whatever came after the last newline: an incomplete line.
        buffer = lines.pop() + buffer

        for line in lines:
            if not line:
                if saw_field:
                    yield SSEEvent(data="\n".join(data_lines), event=event_name, id=event_id)
                data_lines, event_name, event_id, saw_field = [], None, None, False
                continue
            if line.startswith(":"):
                continue  # comment; providers use these as keepalives
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            saw_field = True
            if field == "data":
                data_lines.append(value)
            elif field == "event":
                event_name = value
            elif field == "id":
                event_id = value
            # Any other field (`retry`, or something new) is not the gateway's business.

    # A stream that ends without its final blank line is out of spec, but truncating the
    # last token because of a missing newline is worse than relaying a frame the client's
    # own parser can reject.
    if saw_field:
        logger.debug("upstream stream ended without a trailing blank line")
        yield SSEEvent(data="\n".join(data_lines), event=event_name, id=event_id)


def format_event(data: str) -> str:
    """Render one downstream frame. Multi-line payloads get one ``data:`` line each."""
    return "".join(f"data: {line}\n" for line in data.split("\n")) + "\n"
