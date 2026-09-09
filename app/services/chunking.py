"""Splitting extracted text into the units that get embedded (SPEC §9.3).

A chunk is the smallest thing retrieval can return, so its boundaries decide what a
question can reach. Six strategies, in two groups.

Three cut on a token budget, adjusted for where punctuation happens to be:

``fixed``      exact token windows. No boundary hunting at all.
``recursive``  a token window, then walked back to the nearest paragraph, then sentence,
               then word boundary — in that order, and only if the boundary is not so far
               back that the chunk becomes stunted.
``by_heading`` the document's own sections are the chunks; an oversized one is sub-split
               recursively, and a format with no headings has one section, which is the
               fallback to ``recursive`` the SPEC asks for, arrived at by the shape of the
               data rather than by a special case.

Three cut on something the first three cannot see:

``semantic``        where consecutive sentences stop being about the same thing, measured
                    against this document's own distribution of distances.
``sentence_window`` one sentence embedded, that sentence plus its neighbours returned:
                    a small unit to match on and enough context to answer with.
``code``            function and class bodies as units, with the enclosing declaration
                    carried into each fragment.

**The purity of :func:`chunk_document` is the thing this module protects.** ``semantic``
needs embeddings, which is network I/O, and the obvious way to get them is to make the
splitter async and hand it a client. That would make every strategy untestable without a
fake embedder. Instead the boundary signal is computed *outside* — :func:`plan_signal`
says which spans to embed, :func:`boundary_signal` turns their vectors into per-gap
distances — and passed in. The cost is one extra type; what it buys is a strategy suite
that runs on numbers typed by hand.

A strategy that needs a signal and does not get one raises
:class:`MissingBoundarySignal`. It does **not** quietly fall back to ``recursive``: that
would mean a connector configured for ``semantic`` indexing as something else, with
nothing on any screen saying so — which is precisely the invisible-wrong-value failure
this module's constraints exist to prevent.

Two invariants hold for every strategy, and both are enforced rather than assumed.

**Progress.** Each chunk starts strictly after the previous one. Overlap moves the start
backwards, boundary snapping moves the end backwards, and a configuration that made those
cancel out would loop forever on one document and hold a worker until it was killed.

**Never mid-word** when ``respect_boundaries`` is set. A word cut in half embeds as two
tokens that mean nothing, and the halves are what a reader sees in a citation.

Every offset here is a *character* offset, with token counts derived from
:mod:`app.services.tokenizer`. Doing it the other way — working in token space and
decoding back — makes boundary snapping impossible, because paragraph breaks do not exist
in token space.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.schemas.connector_config import ChunkingConfig, size_floor
from app.services.code_structure import Declaration, declarations
from app.services.extraction import Extracted, Section
from app.services.filetypes import language_of
from app.services.tokenizer import Tokenizer, count, token_index, token_span
from app.services.vector_store import cosine

#: How much of a full-size window a snapped chunk must still fill. Walking back to a
#: paragraph break is worth it; walking back 900 tokens to find one is not — that trades
#: one good boundary for a chunk with almost nothing in it.
MIN_FILL = 0.5

#: Ceiling on the spans ``semantic`` embeds for one document. A 900-page handbook has tens
#: of thousands of sentences, and embedding every one of them to decide where to cut costs
#: more than the document is worth. Past this the sentences are grouped — see
#: :attr:`SignalRequest.stride` — which lowers the *resolution* of the boundaries rather
#: than silently switching the document to another strategy.
MAX_SIGNAL_SPANS = 1500

#: Separators tried in order. The first is what makes prose chunk well; the last is what
#: guarantees "never mid-word" for text with no punctuation at all, such as source code.
_PARAGRAPH = re.compile(r"\n\s*\n")
_SENTENCE = re.compile(r"(?<=[.!?])[\"')\]]*\s")
_LINE = re.compile(r"\n")
_WORD = re.compile(r"\s")

_SEPARATORS = (_PARAGRAPH, _SENTENCE, _LINE, _WORD)


class MissingBoundarySignal(RuntimeError):
    """A strategy asked for a boundary signal and was not given one.

    A programming error, not a user-facing failure: whoever called
    :func:`chunk_document` was told by :func:`needs_signal` that this configuration
    needs one. Loud for the same reason the fallback is refused — see the module
    docstring.
    """


@dataclass(frozen=True, slots=True)
class Chunk:
    """One unit of the index."""

    text: str
    index: int
    #: SPEC §9.3's ``page_or_section``. ``None`` for a document with no structure, which
    #: is honest — an invented label is worse than none in a citation.
    section: str | None
    token_count: int
    #: What is embedded and therefore what a query is matched against. The same string as
    #: ``text`` under every strategy but ``sentence_window``, where the whole proposition
    #: is that they differ: a sentence is the unit worth matching, and the paragraph
    #: around it is the unit worth answering from.
    embedded_text: str = ""

    def __post_init__(self) -> None:
        if not self.embedded_text:
            object.__setattr__(self, "embedded_text", self.text)

    @property
    def windowed(self) -> bool:
        return self.embedded_text != self.text


@dataclass(frozen=True, slots=True)
class Span:
    """A half-open character range of the joined document text, and where it came from.

    ``section`` is an index rather than a title because two sections can share a title and
    a chunk must not silently run across the boundary between them.
    """

    start: int
    end: int
    section: int


@dataclass(frozen=True, slots=True)
class SignalRequest:
    """What has to be embedded before a ``semantic`` document can be cut.

    Returned by :func:`plan_signal` and handed straight back through
    :func:`boundary_signal`, so the spans the distances describe are by construction the
    spans the splitter will use. Recomputing them on the other side is how the two come to
    disagree about where sentence 900 starts.
    """

    text: str
    spans: tuple[Span, ...]
    #: Sentences per span. ``1`` normally; more for a document over
    #: :data:`MAX_SIGNAL_SPANS`, where boundaries are considered every ``stride``
    #: sentences instead of every one.
    stride: int = 1

    @property
    def texts(self) -> list[str]:
        """The strings to embed, in order. Never empty strings — an embedding provider
        rejects those, and a zero vector would have undefined similarity to everything."""
        return [self.text[span.start : span.end].strip() or " " for span in self.spans]

    def __len__(self) -> int:
        return len(self.spans)


@dataclass(frozen=True, slots=True)
class BoundarySignal:
    """Per-gap distances over a document's own spans.

    ``distances[i]`` is the distance between span ``i`` and span ``i + 1``, so there is
    always exactly one fewer of them than there are spans. A cosine *distance*: larger
    means the two spans are about less similar things, which is what a topic change looks
    like from here.
    """

    spans: tuple[Span, ...]
    distances: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.distances) != max(0, len(self.spans) - 1):
            raise ValueError(
                f"{len(self.spans)} spans need {max(0, len(self.spans) - 1)} distances, "
                f"got {len(self.distances)}"
            )


@dataclass(frozen=True, slots=True)
class _Placed:
    """A section, and where its text landed in the joined document."""

    start: int
    end: int
    title: str | None


@dataclass(frozen=True, slots=True)
class _Joined:
    """The document as one string, plus the section each range came from."""

    text: str
    sections: tuple[_Placed, ...] = field(default_factory=tuple)

    def title(self, position: int) -> str | None:
        found: str | None = None
        for placed in self.sections:
            if placed.start > position:
                break
            found = placed.title
        return found


# ---------------------------------------------------------------------------
# what the caller has to do before calling
# ---------------------------------------------------------------------------


def needs_signal(config: ChunkingConfig) -> bool:
    """Whether this configuration cuts using the embedding model.

    The question ingestion asks before it starts, and the question task 17's reindexer
    asks to decide between re-embedding a connector and recutting it.
    """
    return config.strategy == "semantic"


def plan_signal(extracted: Extracted, config: ChunkingConfig) -> SignalRequest:
    """The spans whose vectors ``semantic`` needs, for this document.

    Sentences never straddle a section boundary, whatever ``atomic_sections`` says. Under
    an atomic format the chunker is going to cut there anyway, and a *distance* measured
    across the join would be a comparison between the end of page 4 and the start of
    page 5 — a number that says nothing about either.
    """
    joined = _join(extracted.sections)
    sentences = _sentences(joined)
    stride = max(1, math.ceil(len(sentences) / MAX_SIGNAL_SPANS)) if sentences else 1
    return SignalRequest(text=joined.text, spans=_grouped(sentences, stride), stride=stride)


def boundary_signal(request: SignalRequest, vectors: Sequence[Sequence[float]]) -> BoundarySignal:
    """Consecutive-span cosine distances. Pure — the vectors are the caller's problem.

    Distance, not similarity, and the sign matters more than it looks: everything
    downstream reads "larger means a boundary". Getting it the wrong way round raises
    nothing and cuts a document in exactly the places where it holds together.
    """
    if len(vectors) != len(request.spans):
        raise ValueError(f"{len(request.spans)} spans were planned, {len(vectors)} embedded")
    distances = tuple(
        _distance(vectors[index], vectors[index + 1]) for index in range(len(vectors) - 1)
    )
    return BoundarySignal(spans=request.spans, distances=distances)


def _distance(one: Sequence[float], other: Sequence[float]) -> float:
    """Cosine distance in ``[0, 2]``, from the same cosine the vector store ranks with.

    Reusing :func:`~app.services.vector_store.cosine` rather than writing a second one is
    not tidiness. The whole premise of ``semantic`` is that a boundary is a place where
    retrieval would stop matching, and that is only true if "similar" means here what it
    means at search time.

    A zero vector has no direction, so :func:`cosine` answers ``0.0`` for it, which lands
    here as a distance of ``1.0`` — orthogonal, the least-committed thing to say about a
    pair one of which is empty.
    """
    return 1.0 - cosine(one, other)


# ---------------------------------------------------------------------------
# the splitter
# ---------------------------------------------------------------------------


def chunk_document(
    extracted: Extracted,
    config: ChunkingConfig,
    *,
    tokenizer: Tokenizer,
    media_type: str = "",
    signal: BoundarySignal | None = None,
) -> list[Chunk]:
    """Split an extracted document according to its connector's *effective* configuration.

    ``config`` is already resolved per format by
    :func:`~app.schemas.connector_config.effective`; this function does not look at
    ``overrides``. ``media_type`` is here for one strategy — ``code`` needs to know which
    language it is looking at — and is otherwise unused.

    ``atomic_sections`` is the one thing the *document* gets to override the connector on,
    and only two formats claim it. A slide is a unit somebody authored, and joining two of
    them produces a chunk about two subjects. A PDF page is what a citation names, and a
    chunk running from page 144 to page 147 can cite at most one of those truthfully — an
    end user who turns to the page and does not find the sentence stops believing every
    citation after it. A heading in a Markdown file claims nothing, because choosing about
    headings is exactly what the strategy is for.
    """
    if config.strategy == "semantic":
        if signal is None:
            raise MissingBoundarySignal(
                "the 'semantic' strategy needs a boundary signal; call plan_signal(), "
                "embed its spans, and pass boundary_signal() in"
            )
        pieces = _semantic(extracted, config, tokenizer, signal)
    elif config.strategy == "sentence_window":
        pieces = _sentence_window(extracted, config, tokenizer)
    elif config.strategy == "code" and (language := language_of(media_type)) is not None:
        pieces = _code(extracted, config, tokenizer, language)
    elif config.strategy == "by_heading" or extracted.atomic_sections:
        pieces = _by_heading(extracted, config, tokenizer)
    else:
        pieces = _whole_document(extracted, config, tokenizer)

    return [
        Chunk(
            text=piece.text,
            index=index,
            section=piece.section,
            token_count=count(tokenizer, piece.text),
            embedded_text=piece.embedded,
        )
        for index, piece in enumerate(pieces)
    ]


@dataclass(frozen=True, slots=True)
class _Piece:
    """A chunk before it is numbered and counted."""

    text: str
    section: str | None
    #: Empty means "the same as ``text``", which :class:`Chunk` resolves.
    embedded: str = ""


def _by_heading(extracted: Extracted, config: ChunkingConfig, tokenizer: Tokenizer) -> list[_Piece]:
    """One chunk per section, sub-splitting the ones that do not fit.

    Sections are not merged when they are small. That is the strategy's whole proposition:
    a reader asked for the document's own boundaries, and gluing three short sections into
    one chunk to hit a size target throws away the structure they chose it for.
    """
    pieces: list[_Piece] = []
    for section in extracted.sections:
        text = section.text.strip()
        if not text:
            continue
        offsets = tokenizer.offsets(text)
        if len(offsets) - 1 <= config.chunk_size:
            pieces.append(_Piece(text, section.title))
            continue
        pieces.extend(_Piece(part, section.title) for part in _split(text, offsets, config))
    return pieces


def _whole_document(
    extracted: Extracted, config: ChunkingConfig, tokenizer: Tokenizer
) -> list[_Piece]:
    """Split the document as one run of text, labelling each chunk with the section it
    starts in.

    The joining is what produces full-size chunks; the label is what keeps a citation
    usable. Losing the second would make ``recursive`` the strategy that indexes well and
    attributes badly, for no reason other than how the text was assembled.
    """
    joined = _join(extracted.sections)
    if not joined.text.strip():
        return []
    offsets = tokenizer.offsets(joined.text)
    pieces: list[_Piece] = []
    cursor = 0
    for part in _split(joined.text, offsets, config):
        start = joined.text.find(part, cursor)
        if start < 0:  # only if a splitter ever rewrote its input, which none of them do
            start = cursor
        pieces.append(_Piece(part, joined.title(start)))
        cursor = start + 1
    return pieces


# ---------------------------------------------------------------------------
# semantic
# ---------------------------------------------------------------------------


def _semantic(
    extracted: Extracted,
    config: ChunkingConfig,
    tokenizer: Tokenizer,
    signal: BoundarySignal,
) -> list[_Piece]:
    """Group consecutive spans, cutting where they stop being about the same thing.

    Three things end a group, and the order they are checked in is the strategy:

    1. **A section boundary**, when the format's sections are atomic. Not negotiable.
    2. **The ceiling.** ``chunk_size`` is a maximum here, not a target: a semantic chunk
       runs until the next real boundary *or* until the ceiling, whichever comes first.
    3. **A distance above the percentile breakpoint**, but only once the group is past the
       floor. Without the floor, a page of short declarative sentences becomes one chunk
       per sentence — which is ``sentence_window`` without the window, and worse than
       either of them.
    """
    joined = _join(extracted.sections)
    if not joined.text.strip() or not signal.spans:
        return []
    if signal.spans[-1].end > len(joined.text):
        raise ValueError(
            "the boundary signal was computed for different text than this document — "
            "plan_signal() and chunk_document() must be given the same Extracted"
        )

    offsets = tokenizer.offsets(joined.text)
    threshold = _percentile(signal.distances, config.breakpoint_percentile)
    floor = size_floor(config)
    atomic = extracted.atomic_sections

    groups: list[tuple[int, int]] = []
    start_index = 0
    for index in range(1, len(signal.spans)):
        current = signal.spans[index]
        opening = signal.spans[start_index]
        span_start = opening.start
        held = _tokens(offsets, span_start, signal.spans[index - 1].end)
        would_hold = _tokens(offsets, span_start, current.end)

        crosses = atomic and current.section != opening.section
        over = would_hold > config.chunk_size and held > 0
        breaks = signal.distances[index - 1] > threshold and held >= floor
        if crosses or over or breaks:
            groups.append((start_index, index))
            start_index = index
    groups.append((start_index, len(signal.spans)))

    pieces: list[_Piece] = []
    previous = -1
    for first, stop in groups:
        opening = signal.spans[first]
        closing = signal.spans[stop - 1]
        # Overlap keeps its usual meaning: the chunk opens a fixed number of tokens before
        # the boundary. Clamped past the previous chunk's start, which is the progress
        # invariant — see the module docstring.
        start = max(token_span(offsets, opening.start, -config.overlap), previous + 1)
        if atomic:
            start = max(start, _section_start(joined, opening.section))
        text = joined.text[start : closing.end].strip()
        if not text:
            continue
        previous = start
        title = joined.title(opening.start)
        if _tokens(offsets, start, closing.end) <= config.chunk_size:
            pieces.append(_Piece(text, title))
            continue
        # One span longer than the whole ceiling: a wall of text with no sentence
        # punctuation in it. Sub-split recursively rather than emit a chunk the embedding
        # model will refuse.
        pieces.extend(_Piece(part, title) for part in _split(text, tokenizer.offsets(text), config))
    return pieces


def _percentile(values: Sequence[float], percentile: int) -> float:
    """Nearest-rank, over this document's own distances.

    ``inf`` for a document with no gaps at all, so the "is this a boundary" test below is
    false rather than accidentally true for a one-sentence document.

    A *strictly greater* comparison against this is what makes a flat distribution produce
    no cuts: if every gap is the same size, none of them exceeds the percentile, and a
    document that never changes subject is correctly left in one piece.
    """
    if not values:
        return math.inf
    ordered = sorted(values)
    rank = math.ceil(percentile / 100 * len(ordered))
    return ordered[min(max(rank - 1, 0), len(ordered) - 1)]


# ---------------------------------------------------------------------------
# sentence window
# ---------------------------------------------------------------------------


def _sentence_window(
    extracted: Extracted, config: ChunkingConfig, tokenizer: Tokenizer
) -> list[_Piece]:
    """Embed a sentence; keep the sentence plus ``window_sentences`` neighbours as text.

    ``overlap`` is not used and is not silently applied: the window *is* the overlap, and
    adding another would put the same sentence in four chunks instead of three.

    The window never crosses a section boundary. Under an atomic format that is the same
    promise the other strategies make about pages; under a structured one it is what keeps
    the chunk's ``page_or_section`` label true of all of it rather than of its first line.
    """
    joined = _join(extracted.sections)
    spans = _sentences(joined)
    if not spans:
        return []

    offsets = tokenizer.offsets(joined.text)
    reach = config.window_sentences
    pieces: list[_Piece] = []
    for index, span in enumerate(spans):
        sentence = joined.text[span.start : span.end].strip()
        if not sentence:
            continue
        title = joined.title(span.start)
        if _tokens(offsets, span.start, span.end) > config.chunk_size:
            # A "sentence" longer than the ceiling is a wall of text with no punctuation.
            # Sub-split it and let each part stand for itself: a window around a piece
            # that is already too big to embed helps nobody.
            pieces.extend(
                _Piece(part, title)
                for part in _split(sentence, tokenizer.offsets(sentence), config)
            )
            continue
        first = _reach(spans, index, -reach, span.section)
        last = _reach(spans, index, reach, span.section)
        window = joined.text[spans[first].start : spans[last].end].strip()
        pieces.append(_Piece(window or sentence, title, embedded=sentence))
    return pieces


def _reach(spans: Sequence[Span], index: int, offset: int, section: int) -> int:
    """The furthest neighbour in one direction that is still in the same section."""
    step = 1 if offset > 0 else -1
    found = index
    for _ in range(abs(offset)):
        following = found + step
        if not 0 <= following < len(spans) or spans[following].section != section:
            break
        found = following
    return found


# ---------------------------------------------------------------------------
# code
# ---------------------------------------------------------------------------


def _code(
    extracted: Extracted, config: ChunkingConfig, tokenizer: Tokenizer, language: str
) -> list[_Piece]:
    """Declarations as units, with the enclosing signature carried into each fragment.

    A file that will not parse falls through to ``recursive``. That is the whole
    degradation story for this strategy and it is deliberate: a syntax error in one file of
    a repository must cost a worse chunking of that file, never a ``failed`` document. The
    same path covers a language nothing here parses and a minified bundle whose
    declarations are all on line 1.
    """
    joined = _join(extracted.sections)
    if not joined.text.strip():
        return []
    found = declarations(joined.text, language)
    if not found:
        return _whole_document(extracted, config, tokenizer)

    offsets = tokenizer.offsets(joined.text)
    pieces: list[_Piece] = []
    for declaration in found:
        pieces.extend(_declaration(joined, declaration, config, tokenizer, offsets, header=""))
    return pieces


def _declaration(
    joined: _Joined,
    declaration: Declaration,
    config: ChunkingConfig,
    tokenizer: Tokenizer,
    offsets: list[int],
    *,
    header: str,
) -> list[_Piece]:
    """One declaration, descending into its children only when it does not fit.

    A class small enough to embed whole stays whole: its methods are about each other, and
    splitting a 40-line class into six chunks makes six worse answers out of one good one.
    """
    text = joined.text[declaration.start : declaration.end].strip()
    if not text:
        return []
    label = declaration.name
    tokens = _tokens(offsets, declaration.start, declaration.end)
    if tokens <= config.chunk_size:
        return [_Piece(_with_header(header, text), label)]

    if declaration.children:
        pieces: list[_Piece] = []
        cursor = declaration.start
        for child in declaration.children:
            # Whatever sits between two methods — a class-level constant, a docstring —
            # is part of the class and is not silently dropped on the way past.
            between = joined.text[cursor : child.start].strip()
            if between:
                pieces.extend(
                    _Piece(_with_header(header or declaration.header, part), label)
                    for part in _split(between, tokenizer.offsets(between), config)
                )
            pieces.extend(
                _declaration(
                    joined,
                    child,
                    config,
                    tokenizer,
                    offsets,
                    header=_with_header(header, declaration.header),
                )
            )
            cursor = child.end
        trailing = joined.text[cursor : declaration.end].strip()
        if trailing:
            pieces.extend(
                _Piece(_with_header(header or declaration.header, part), label)
                for part in _split(trailing, tokenizer.offsets(trailing), config)
            )
        return pieces

    # A single function longer than the ceiling. Sub-split it, and carry the signature
    # into every part: a fragment of a function body with no signature above it is a
    # citation nobody can place.
    carried = _with_header(header, declaration.header)
    budget = max(1, config.chunk_size - count(tokenizer, carried))
    narrowed = config.model_copy(
        update={"chunk_size": max(budget, 1), "overlap": min(config.overlap, budget // 2)}
    )
    return [
        _Piece(_with_header(carried, part), label)
        for part in _split(text, tokenizer.offsets(text), narrowed)
    ]


def _with_header(header: str, text: str) -> str:
    """Prefix a fragment with its enclosing declaration, without repeating it.

    The check is against the fragment's opening lines rather than against the whole
    header, because the first fragment of a decorated function starts at the decorator and
    reaches the ``def`` a line or two later — and a header pasted above a copy of itself is
    what a reader would call a bug.
    """
    header = header.strip()
    if not header:
        return text
    opening = header.splitlines()[0].strip()
    if any(line.strip() == opening for line in text.splitlines()[:4]):
        return text
    return f"{header}\n{text}"


# ---------------------------------------------------------------------------
# shared machinery
# ---------------------------------------------------------------------------


def _join(sections: tuple[Section, ...]) -> _Joined:
    parts: list[str] = []
    placed: list[_Placed] = []
    position = 0
    for section in sections:
        text = section.text.strip()
        if not text:
            continue
        placed.append(_Placed(start=position, end=position + len(text), title=section.title))
        parts.append(text)
        position += len(text) + 2  # the "\n\n" that joins them
    return _Joined(text="\n\n".join(parts), sections=tuple(placed))


def _sentences(joined: _Joined) -> tuple[Span, ...]:
    """Sentence spans over the joined text, never crossing a section boundary.

    The separator is :data:`_SENTENCE`, the same one ``recursive`` snaps to. Two different
    notions of "sentence" in one module is a bug waiting to be written: a boundary that
    ``semantic`` chose and ``recursive`` would not snap to is a difference nobody could
    explain from the settings screen.
    """
    if not joined.sections:
        stripped = joined.text.strip()
        return (Span(0, len(joined.text), 0),) if stripped else ()

    spans: list[Span] = []
    for index, placed in enumerate(joined.sections):
        cursor = placed.start
        body = joined.text[placed.start : placed.end]
        for match in _SENTENCE.finditer(body):
            end = placed.start + match.end()
            if end > cursor:
                spans.append(Span(cursor, end, index))
                cursor = end
        if cursor < placed.end:
            spans.append(Span(cursor, placed.end, index))
    return tuple(spans)


def _grouped(spans: tuple[Span, ...], stride: int) -> tuple[Span, ...]:
    """Merge runs of ``stride`` sentences, never across a section boundary."""
    if stride <= 1:
        return spans
    merged: list[Span] = []
    held: list[Span] = []
    for span in spans:
        if held and (span.section != held[0].section or len(held) >= stride):
            merged.append(Span(held[0].start, held[-1].end, held[0].section))
            held = []
        held.append(span)
    if held:
        merged.append(Span(held[0].start, held[-1].end, held[0].section))
    return tuple(merged)


def _section_start(joined: _Joined, section: int) -> int:
    return joined.sections[section].start if 0 <= section < len(joined.sections) else 0


def _tokens(offsets: list[int], start: int, end: int) -> int:
    return max(0, token_index(offsets, end) - token_index(offsets, start))


def _split(text: str, offsets: list[int], config: ChunkingConfig) -> list[str]:
    """The token-window splitter the boundary strategies delegate to."""
    total_tokens = len(offsets) - 1
    if total_tokens <= config.chunk_size:
        stripped = text.strip()
        return [stripped] if stripped else []

    snap = config.respect_boundaries and config.strategy != "fixed"
    chunks: list[str] = []
    start = 0
    guard = total_tokens * 2 + 16  # see the progress invariant in the module docstring

    while start < len(text) and guard > 0:
        guard -= 1
        limit = token_span(offsets, start, config.chunk_size)
        end = len(text) if limit >= len(text) else _boundary(text, start, limit, snap=snap)

        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break

        following = token_span(offsets, end, -config.overlap)
        # Progress, unconditionally: overlap that reaches back past this chunk's start
        # would re-split the same window forever.
        start = following if following > start else end

    return chunks


def _boundary(text: str, start: int, limit: int, *, snap: bool) -> int:
    """Where to cut, at or before ``limit``.

    Returns ``limit`` itself when no acceptable boundary exists, which is the honest
    answer for a 5000-character line of minified JSON: there is nowhere better, and
    refusing to cut would produce a chunk the embedding model rejects.
    """
    if not snap:
        return limit

    floor = start + int((limit - start) * MIN_FILL)
    window = text[start:limit]
    for separator in _SEPARATORS:
        matches = list(separator.finditer(window))
        if not matches:
            continue
        # `end()`, so the separator stays with the chunk that precedes it and the next
        # chunk does not open with a newline.
        candidate = start + matches[-1].end()
        if candidate > floor:
            return candidate
    return limit


__all__ = [
    "MAX_SIGNAL_SPANS",
    "MIN_FILL",
    "BoundarySignal",
    "Chunk",
    "MissingBoundarySignal",
    "SignalRequest",
    "Span",
    "boundary_signal",
    "chunk_document",
    "needs_signal",
    "plan_signal",
]
