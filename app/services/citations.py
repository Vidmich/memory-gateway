"""Honouring the citation instruction on the way back (task 100).

SPEC §7 renders every retrieved chunk under a numbered handle — ``[2] source: handbook.pdf
(p. 12)`` — and tells the model to cite them. Models do. Until this module the gateway
forwarded ``as [2] notes`` verbatim to a client that had never seen ``[2]``, and the log
recorded which chunks were *injected* while nothing recorded which were *used*.

Three pieces, in the order a request meets them.

**Resolution is pure.** :func:`resolve` takes the answer text and the chunks the assembler
numbered, and returns which handles pointed at a chunk, which pointed at nothing, and where
every handle sits in the text. No I/O and no state, which is what lets the ``off`` path
call it on every request for the log at a cost measured in microseconds, and what makes
the grammar testable against an adversarial corpus without a provider.

**The numbering is the assembler's, never a recount.** ``injected`` is
:attr:`~app.services.prompt.Assembled.injected` in the order :func:`~app.services.prompt.
render_entry` printed it, so a chunk that ``fit_documents`` dropped from the tail cannot
resolve and ``[2]`` here is the ``[2]`` the model saw. Recounting from the retrieval
result would agree until the day the budget dropped one — which is the day it matters.

**Streaming buffers only the tail.** A frame ending in ``[`` may be the start of a handle
or a literal bracket, and the only way to know is to wait for the next frame. So the
scanner holds back at most a partial handle's worth of characters and forwards everything
before it unchanged. It is the cost of stripping hallucinated handles under ``footer``, and
it is paid nowhere else: under ``metadata`` and ``off`` the text is never rewritten, so
frames pass through byte for byte and time-to-first-token is what the provider made it.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

from app.schemas.openai import ChatChunk, ChatResponse, StreamFrame

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, same as prompt.py
    from app.services.retrieval import Chunk

MODE_OFF = "off"
MODE_METADATA = "metadata"
MODE_FOOTER = "footer"
type CitationMode = Literal["off", "metadata", "footer"]

#: A range separator: a hyphen, or the en dash (U+2013) models emit when being tidy.
_DASH = "\\-\u2013"
#: How a handle opens: a bracket not glued to a word — so ``arr[0]`` in prose is an
#: index and not a citation of chunk zero, the same reason fenced code is skipped whole,
#: applied to the one-token case a fence does not cover — or the footnote form ``[^``,
#: which *is* glued to the word it annotates.
_OPEN = r"(?:(?<!\w)\[|\[\^)"
#: The handle grammar, as models actually write it: ``[2]``, ``[2, 3]``, ``[2-4]``,
#: ``[^2]``, and ``[2][3]`` as two adjacent matches. Three digits at most: no gateway
#: injects a thousand chunks, and a longer number in brackets is a year or an amount.
HANDLE = re.compile(rf"{_OPEN}(\d{{1,3}}(?:\s*[,{_DASH}]\s*\d{{1,3}})*)\]")
#: A tail of text that may still become a handle once more of it arrives — what the
#: streaming scanner holds back. The optional space before the bracket is held with it,
#: so that when the handle turns out to be hallucinated the space goes with it and the
#: client never sees ``said  now`` where the model wrote ``said [7] now``.
PARTIAL = re.compile(rf" ?{_OPEN}[\d\s,{_DASH}]*$")
#: A fenced code block's opening or closing line. Anything between two of these is code,
#: and ``[0]`` inside it is an array index however much it looks like a citation.
FENCE = re.compile(r"^ {0,3}(```|~~~)")
#: Longest tail the streaming scanner will hold back waiting for a closing bracket.
MAX_PENDING = 64
#: Ranges longer than this are not citations of consecutive chunks; ``[1-100]`` is a
#: page range or a score, and expanding it would report ninety-odd unresolved handles.
MAX_RANGE = 20

#: Longest answer the streaming scanner keeps for resolution. The same ceiling as the
#: request log's tee, for the same reason: a runaway generation must cost a marker, not
#: the process.
MAX_TEXT_CHARS = 256_000

FOOTER_HEADING = "Sources:"

#: What may follow a removed handle for the space before it to go too: punctuation,
#: whitespace, a closing bracket. Before a word — or another handle — the space stays.
_SPACE_MAY_GO_BEFORE = frozenset(" \t\n\r.,;:!?)]}")


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Span:
    """One handle in the text, with which of its numbers had a chunk behind them.

    ``[2, 7]`` with six chunks injected is one span, ``resolved=(2,)``,
    ``unresolved=(7,)``; its :meth:`rewrite` is ``[2]``. A span with nothing resolved
    rewrites to the empty string — a ``[7]`` with no source listed under it is worse than
    no marker, which is why ``footer`` strips them.
    """

    start: int
    end: int
    numbers: tuple[int, ...]
    resolved: tuple[int, ...]
    unresolved: tuple[int, ...]

    def rewrite(self) -> str:
        if not self.resolved:
            return ""
        return "[" + ", ".join(str(number) for number in self.resolved) + "]"


def scan(text: str) -> list[tuple[int, int, tuple[int, ...]]]:
    """Every handle in ``text`` outside fenced code, as ``(start, end, numbers)``.

    Line by line, because a fence is a line-level construct: the state flips on a line
    that opens or closes one and every line between is skipped whole. Inline code —
    a backticked ``arr[0]`` mid-sentence — is not skipped, and the word-boundary rule in
    :data:`HANDLE` is what keeps that case honest instead.
    """
    found: list[tuple[int, int, tuple[int, ...]]] = []
    in_fence = False
    offset = 0
    for line in text.splitlines(keepends=True):
        if FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            last_end = -1
            for match in HANDLE.finditer(line):
                start = match.start()
                # ``[2][3]`` is two handles; ``matrix[1][2]`` is two indexes. The second
                # bracket of each is preceded by ``]``, and what tells them apart is
                # whether the *first* was a handle: a chain only continues one.
                if start > 0 and line[start - 1] == "]" and last_end != start:
                    continue
                numbers = _numbers(match.group(1))
                if numbers:
                    found.append((offset + start, offset + match.end(), numbers))
                    last_end = match.end()
        offset += len(line)
    return found


def _numbers(group: str) -> tuple[int, ...]:
    """``"2, 3"`` → ``(2, 3)``; ``"2-4"`` → ``(2, 3, 4)``; a reversed or absurd range
    is two numbers rather than none, so the handle still counts as written."""
    numbers: list[int] = []
    for part in re.split(r"\s*,\s*", group.strip()):
        bounds = re.split(rf"\s*[{_DASH}]\s*", part)
        if len(bounds) == 2:
            start, end = int(bounds[0]), int(bounds[1])
            if start <= end <= start + MAX_RANGE:
                numbers.extend(range(start, end + 1))
            else:
                numbers.extend((start, end))
        else:
            numbers.append(int(bounds[0]))
    return tuple(numbers)


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Citation:
    """A handle that pointed at a chunk: what the client is told about it.

    Nothing here is text the client has not already been sent inside the prompt — the
    document name and the section were in the excerpt's own heading. It is the same
    information, structured, plus the ids that let a control-plane link be built.
    """

    handle: int
    chunk: Chunk

    def as_json(self, *, base_url: str | None = None) -> dict[str, Any]:
        chunk = self.chunk
        return {
            "handle": self.handle,
            "chunk_id": chunk.id,
            "document_id": chunk.document_id,
            "document_name": chunk.source_name,
            "connector_id": chunk.connector_id,
            "section": chunk.page_or_section,
            "chunk_strategy": chunk.chunk_strategy,
            # The sentence that matched under ``sentence_window``, not the window around
            # it — the one strategy where "what was cited" and "what was shown" differ.
            "matched_text": chunk.matched_text,
            "url": inspector_url(base_url, chunk),
        }

    def footer_line(self, *, base_url: str | None = None) -> str:
        """``[2] handbook.pdf (p. 12)`` — the handle the model wrote, never renumbered,
        and the section in the shape the prompt's own heading printed it."""
        chunk = self.chunk
        where = f" ({chunk.page_or_section})" if chunk.page_or_section else ""
        label = f"{chunk.source_name}{where}"
        url = inspector_url(base_url, chunk)
        # A Markdown link for the clients that render one; a terminal shows the URL, which
        # is still the right answer to "where did this come from".
        return f"[{self.handle}] [{label}]({url})" if url else f"[{self.handle}] {label}"


@dataclass(frozen=True, slots=True)
class Resolution:
    """What :func:`resolve` found: the cited chunks, the handles with nothing behind
    them, and every span — so ``footer`` can strip and the log can count."""

    cited: tuple[Citation, ...] = ()
    #: Distinct, in order of first appearance. ``[7]`` with six chunks injected.
    unresolved: tuple[int, ...] = ()
    spans: tuple[Span, ...] = ()

    @property
    def cited_ids(self) -> list[str]:
        return [citation.chunk.id for citation in self.cited]

    def strip(self, text: str, *, from_offset: int = 0) -> str:
        """``text`` with every unresolved number removed from its handle.

        Spans are rewritten from the end so earlier offsets stay valid. A handle that
        loses all its numbers disappears along with one space before it, so ``says [7].``
        becomes ``says.`` rather than ``says .``.

        ``from_offset`` is the streaming scanner's: text before it has already been sent
        and cannot change, so spans there are left alone and the space rule stops at it.
        """
        return self.strip_deferring(text, from_offset=from_offset)[0]

    def strip_deferring(self, text: str, *, from_offset: int = 0) -> tuple[str, bool]:
        """:meth:`strip`, plus whether a space *after* ``text`` should be dropped.

        The second value is for the streaming scanner. When a handle is removed and the
        space before it was already sent, the space after it is taken instead — and if
        that space has not arrived yet, the scanner is told to drop it when it does.
        """
        result = text
        eat_next = False
        for span in reversed(self.spans):
            if not span.unresolved or span.start < from_offset:
                continue
            replacement = span.rewrite()
            start, end = span.start, span.end
            if not replacement:
                follow = result[end] if end < len(result) else None
                if follow is not None and follow not in _SPACE_MAY_GO_BEFORE:
                    # ``[8][2]`` → ``[2]``: what follows needs the space in front of it.
                    pass
                elif start > from_offset and result[start - 1] == " ":
                    start -= 1
                elif follow == " ":
                    end += 1
                elif follow is None:
                    eat_next = True
            result = result[:start] + replacement + result[end:]
        return result, eat_next

    def merge(self, other: Resolution) -> Resolution:
        """Two choices of one response, as one record. Spans are per text and do not
        combine; the cited list and the unresolved count do."""
        seen = {citation.chunk.id for citation in self.cited}
        cited = list(self.cited) + [c for c in other.cited if c.chunk.id not in seen]
        unresolved = tuple(dict.fromkeys((*self.unresolved, *other.unresolved)))
        return Resolution(cited=tuple(cited), unresolved=unresolved, spans=self.spans)


def resolve(text: str | None, injected: Sequence[Chunk]) -> Resolution:
    """Which of ``injected`` the answer cites, by the handles the prompt gave them.

    Pure. ``cited`` is deduplicated and in order of first appearance, because "the
    answer leaned on [3] first" is a fact worth keeping and "[3] was cited four times" is
    not. A handle whose number exceeds ``len(injected)`` — or is zero — has nothing behind
    it and is unresolved.
    """
    if not text:
        return Resolution()
    cited: dict[int, Citation] = {}
    unresolved: dict[int, None] = {}
    spans: list[Span] = []
    for start, end, numbers in scan(text):
        resolved_here: list[int] = []
        unresolved_here: list[int] = []
        for number in numbers:
            if 1 <= number <= len(injected):
                resolved_here.append(number)
                cited.setdefault(number, Citation(handle=number, chunk=injected[number - 1]))
            else:
                unresolved_here.append(number)
                unresolved.setdefault(number)
        spans.append(
            Span(
                start=start,
                end=end,
                numbers=numbers,
                resolved=tuple(dict.fromkeys(resolved_here)),
                unresolved=tuple(dict.fromkeys(unresolved_here)),
            )
        )
    return Resolution(
        cited=tuple(cited.values()),
        unresolved=tuple(unresolved),
        spans=tuple(spans),
    )


def inspector_url(base_url: str | None, chunk: Chunk) -> str | None:
    """The control plane's chunk inspector, opened at this chunk.

    ``None`` when the deployment has no UI address or the chunk has no connector to be
    found under — a payload repaired by hand, or a test fixture — rather than a link that
    404s. The chunk id is query-quoted because it is ``{document_id}:{index}`` and the
    colon is legal in a query string but worth not arguing about with every proxy.
    """
    if not base_url or not chunk.connector_id or not chunk.document_id:
        return None
    root = base_url.rstrip("/")
    return (
        f"{root}/connectors/{chunk.connector_id}"
        f"?document={chunk.document_id}&chunk={quote(chunk.id, safe='')}"
    )


def footer(citations: Iterable[Citation], *, base_url: str | None = None) -> str:
    """The ``footer`` mode's addition to the content, or the empty string.

    Handles are the model's own, in the order the answer first used them. Not renumbered:
    a tidy ``1..k`` list would make ``[3]`` in the text and ``[2]`` in the footer the same
    chunk, and nobody reading it could tell.
    """
    lines = [citation.footer_line(base_url=base_url) for citation in citations]
    if not lines:
        return ""
    return "\n\n" + "\n".join([FOOTER_HEADING, *lines])


# ---------------------------------------------------------------------------
# non-streaming delivery
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Delivered:
    response: ChatResponse
    resolution: Resolution


def deliver(
    response: ChatResponse,
    injected: Sequence[Chunk],
    *,
    mode: str,
    base_url: str | None = None,
) -> Delivered:
    """Resolve every choice's citations and, depending on ``mode``, tell the client.

    ``off`` returns the response object it was given, untouched — byte-identity with
    today's output is an acceptance criterion, and the cheapest way to guarantee it is
    not to build a new object. The resolution is still computed and returned, because
    the log records it whatever the mode.
    """
    if not injected or not response.choices:
        return Delivered(response=response, resolution=Resolution())

    combined: Resolution | None = None
    for choice in response.choices:
        message = choice.message
        content = message.content if message is not None else None
        if message is None or not isinstance(content, str):
            continue
        resolution = resolve(content, injected)
        combined = resolution if combined is None else combined.merge(resolution)
        if mode == MODE_METADATA:
            # Extra fields on a model with ``extra="allow"``: what every OpenAI SDK
            # ignores and every hand-written client can read.
            message.citations = [c.as_json(base_url=base_url) for c in resolution.cited]  # type: ignore[attr-defined]
            message.citations_unresolved = list(resolution.unresolved)  # type: ignore[attr-defined]
        elif mode == MODE_FOOTER:
            message.content = resolution.strip(content) + footer(
                resolution.cited, base_url=base_url
            )
    return Delivered(response=response, resolution=combined or Resolution())


# ---------------------------------------------------------------------------
# streaming delivery
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ChoiceText:
    """What one choice has said so far, and the tail not yet forwarded."""

    parts: list[str] = field(default_factory=list)
    length: int = 0
    truncated: bool = False
    #: ``footer`` only: characters held back because they may be the start of a handle.
    pending: str = ""
    #: ``footer`` only: whether the forwarded text is currently inside a code fence, and
    #: the partial line the fence detector has not yet seen the end of.
    in_fence: bool = False
    line: str = ""
    #: ``footer`` only: a handle was removed at the very end of what was forwarded, its
    #: preceding space had already gone out, and the next space to arrive is to be
    #: dropped in its place.
    eat_space: bool = False

    def remember(self, text: str) -> None:
        if self.truncated:
            return
        room = MAX_TEXT_CHARS - self.length
        if len(text) > room:
            text = text[:room]
            self.truncated = True
        self.parts.append(text)
        self.length += len(text)

    @property
    def text(self) -> str:
        return "".join(self.parts)


class StreamCitations:
    """Relays a stream's frames, resolving citations as the answer accumulates.

    :meth:`feed` returns what to send downstream for one upstream frame — usually the
    frame itself, untouched. :meth:`finish` returns what to add after the upstream's last
    frame: the footer delta or the metadata chunk. :meth:`resolution` is the record for
    the log, and it is valid at any moment, so a stream the client abandoned still gets
    one over what had arrived.

    Under ``off`` and ``metadata`` the frames are never rewritten: ``feed`` hands back
    the very object it was given. Under ``footer`` a frame is rebuilt only when its text
    changed — a tail held back, a hallucinated handle removed — so the common frame is
    still the provider's own bytes.
    """

    def __init__(
        self,
        injected: Sequence[Chunk],
        *,
        mode: str,
        base_url: str | None = None,
    ) -> None:
        self._injected = tuple(injected)
        self._mode = mode
        self._base_url = base_url
        self._choices: dict[int, _ChoiceText] = {}
        #: The last parsed chunk, so the frames this class adds carry the same id, model
        #: and ``created`` as the provider's own and a client grouping by id keeps them.
        self._last: ChatChunk | None = None

    # -- relay -------------------------------------------------------------

    def feed(self, frame: StreamFrame) -> list[StreamFrame]:
        chunk = frame.chunk
        if chunk is None or not self._injected:
            return [frame]
        self._last = chunk

        rewritten = False
        for choice in chunk.choices:
            content = choice.delta.content
            if not content:
                continue
            state = self._choices.setdefault(choice.index, _ChoiceText())
            state.remember(content)
            if self._mode != MODE_FOOTER:
                continue
            forwarded = self._forward(state, content)
            if forwarded != content:
                choice.delta.content = forwarded
                rewritten = True

        if not rewritten:
            return [frame]
        if not _carries_anything(chunk):
            # Everything this frame said is being held back. Sending an empty delta
            # would be harmless and pointless; the text arrives with the next frame.
            return []
        return [_reserialise(chunk)]

    def finish(self) -> list[StreamFrame]:
        """What follows the provider's last frame and precedes ``[DONE]``."""
        if not self._injected or self._last is None:
            return []
        frames: list[StreamFrame] = []
        if self._mode == MODE_FOOTER:
            for index, state in sorted(self._choices.items()):
                # Whatever is still pending is literal text: no more frames are coming
                # to complete it into a handle.
                tail = self._flush(state)
                addition = tail + footer(
                    resolve(state.text, self._injected).cited, base_url=self._base_url
                )
                if addition:
                    frames.append(self._delta(index, {"content": addition}))
        elif self._mode == MODE_METADATA:
            for index, state in sorted(self._choices.items()):
                resolution = resolve(state.text, self._injected)
                frames.append(
                    self._delta(
                        index,
                        {
                            "citations": [
                                c.as_json(base_url=self._base_url) for c in resolution.cited
                            ],
                            "citations_unresolved": list(resolution.unresolved),
                        },
                    )
                )
        return frames

    def resolution(self) -> Resolution:
        """The record, over every choice's text so far."""
        combined: Resolution | None = None
        for _, state in sorted(self._choices.items()):
            resolution = resolve(state.text, self._injected)
            combined = resolution if combined is None else combined.merge(resolution)
        return combined or Resolution()

    # -- footer: the tail buffer ----------------------------------------------

    def _forward(self, state: _ChoiceText, content: str) -> str:
        """The part of ``pending + content`` that is safe to send now, stripped.

        Safe means: no suffix that :data:`PARTIAL` could still complete into a handle.
        Everything before that suffix is final, so unresolved handles in it can be
        removed here and never seen by the client.
        """
        text = state.pending + content
        held = PARTIAL.search(text)
        # A tail longer than any handle is a list of numbers, not a citation in progress;
        # holding it would delay text for no decision that is ever going to be made.
        cut = held.start() if held and len(text) - held.start() <= MAX_PENDING else len(text)
        ready, state.pending = text[:cut], text[cut:]
        return self._strip(state, ready)

    def _flush(self, state: _ChoiceText) -> str:
        ready, state.pending = state.pending, ""
        return self._strip(state, ready)

    def _strip(self, state: _ChoiceText, text: str) -> str:
        """Remove unresolved handles from ``text``, tracking code fences across frames.

        Two pieces of state survive between calls, because a stream does not arrive in
        lines. The fence flag: a block opened three frames ago is still open. And the
        current line so far: a fence marker can arrive one backtick per frame, and the
        word-boundary rule in :data:`HANDLE` needs the character before a handle even
        when that character was sent in the previous frame — otherwise ``arr`` and then
        ``[0]`` would be stripped here and counted as nothing by :func:`resolve` over the
        whole text, and the log and the client would disagree.
        """
        if not text:
            return text
        out: list[str] = []
        eat_next = False
        for piece in re.split(r"(?<=\n)", text):
            if not piece:
                continue
            context = state.line
            state.line += piece
            complete = piece.endswith("\n")
            if FENCE.match(state.line):
                # The marker itself. Nothing on it is a citation, and the state flips
                # for the lines after it — once the line is known to be complete.
                if complete:
                    state.in_fence = not state.in_fence
                sent = piece
            elif state.in_fence:
                sent = piece
            else:
                # Resolved with the already-sent part of the line as context, then cut
                # back to the new part: nothing before ``from_offset`` changes, so the
                # slice is exact.
                whole = context + piece
                stripped, eat_next = resolve(whole, self._injected).strip_deferring(
                    whole, from_offset=len(context)
                )
                sent = stripped[len(context) :]
            if state.eat_space and sent:
                # A handle was removed at the very end of the previous piece and the
                # space before it had already gone out. If this piece opens with the
                # space after it, that one goes instead; whatever it opens with, the
                # moment has passed.
                if sent[0] == " ":
                    sent = sent[1:]
                state.eat_space = False
            state.eat_space = state.eat_space or eat_next
            eat_next = False
            out.append(sent)
            if complete:
                state.line = ""
        return "".join(out)

    # -- frames this class adds ------------------------------------------------

    def _delta(self, index: int, delta: dict[str, Any]) -> StreamFrame:
        last = self._last
        payload = {
            "id": last.id if last is not None else "",
            "object": "chat.completion.chunk",
            "created": last.created if last is not None else int(time.time()),
            "model": last.model if last is not None else "",
            "choices": [{"index": index, "delta": delta, "finish_reason": None}],
        }
        return StreamFrame(data=json.dumps(payload), chunk=ChatChunk.model_validate(payload))


def _carries_anything(chunk: ChatChunk) -> bool:
    """Whether a rewritten frame still says something a client needs."""
    if chunk.usage is not None:
        return True
    for choice in chunk.choices:
        if choice.finish_reason is not None or choice.delta.role or choice.delta.content:
            return True
        # A field this code has never heard of — tool calls, refusals — is kept.
        if choice.delta.model_extra:
            return True
    return False


def _reserialise(chunk: ChatChunk) -> StreamFrame:
    """The provider's frame with the gateway's content, everything else preserved.

    ``exclude_none`` so the frame reads like the provider's rather than a schema dump —
    the same rule the request log applies to stored messages. Unknown fields ride along
    because every model in :mod:`app.schemas.openai` allows extras.
    """
    return StreamFrame(data=json.dumps(chunk.model_dump(exclude_none=True)), chunk=chunk)


__all__ = [
    "FOOTER_HEADING",
    "HANDLE",
    "MODE_FOOTER",
    "MODE_METADATA",
    "MODE_OFF",
    "Citation",
    "CitationMode",
    "Delivered",
    "Resolution",
    "Span",
    "StreamCitations",
    "deliver",
    "footer",
    "inspector_url",
    "resolve",
    "scan",
]
