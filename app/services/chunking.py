"""Splitting extracted text into the units that get embedded (SPEC §9.3).

A chunk is the smallest thing retrieval can return, so its boundaries decide what a
question can reach. Three strategies, and the differences between them are about *where*
a cut is allowed, never about how big the pieces are:

``fixed``      exact token windows. No boundary hunting at all.
``recursive``  a token window, then walked back to the nearest paragraph, then sentence,
               then word boundary — in that order, and only if the boundary is not so far
               back that the chunk becomes stunted.
``by_heading`` the document's own sections are the chunks; an oversized one is sub-split
               recursively, and a format with no headings has one section, which is the
               fallback to ``recursive`` the SPEC asks for, arrived at by the shape of the
               data rather than by a special case.

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

import re
from dataclasses import dataclass

from app.schemas.connector_config import ChunkingConfig
from app.services.extraction import Extracted, Section
from app.services.tokenizer import Tokenizer, count, token_span

#: How much of a full-size window a snapped chunk must still fill. Walking back to a
#: paragraph break is worth it; walking back 900 tokens to find one is not — that trades
#: one good boundary for a chunk with almost nothing in it.
MIN_FILL = 0.5

#: Separators tried in order. The first is what makes prose chunk well; the last is what
#: guarantees "never mid-word" for text with no punctuation at all, such as source code.
_PARAGRAPH = re.compile(r"\n\s*\n")
_SENTENCE = re.compile(r"(?<=[.!?])[\"')\]]*\s")
_LINE = re.compile(r"\n")
_WORD = re.compile(r"\s")

_SEPARATORS = (_PARAGRAPH, _SENTENCE, _LINE, _WORD)


@dataclass(frozen=True, slots=True)
class Chunk:
    """One unit of the index."""

    text: str
    index: int
    #: SPEC §9.3's ``page_or_section``. ``None`` for a document with no structure, which
    #: is honest — an invented label is worse than none in a citation.
    section: str | None
    token_count: int


def chunk_document(
    extracted: Extracted,
    config: ChunkingConfig,
    *,
    tokenizer: Tokenizer,
) -> list[Chunk]:
    """Split an extracted document according to its connector's configuration.

    ``atomic_sections`` is the one thing the *document* gets to override the connector on,
    and only two formats claim it. A slide is a unit somebody authored, and joining two of
    them produces a chunk about two subjects. A PDF page is what a citation names, and a
    chunk running from page 144 to page 147 can cite at most one of those truthfully — an
    end user who turns to the page and does not find the sentence stops believing every
    citation after it. A heading in a Markdown file claims nothing, because choosing about
    headings is exactly what the strategy is for.
    """
    if config.strategy == "by_heading" or extracted.atomic_sections:
        pieces = _by_heading(extracted, config, tokenizer)
    else:
        pieces = _whole_document(extracted, config, tokenizer)

    return [
        Chunk(
            text=text,
            index=index,
            section=section,
            token_count=count(tokenizer, text),
        )
        for index, (text, section) in enumerate(pieces)
    ]


def _by_heading(
    extracted: Extracted, config: ChunkingConfig, tokenizer: Tokenizer
) -> list[tuple[str, str | None]]:
    """One chunk per section, sub-splitting the ones that do not fit.

    Sections are not merged when they are small. That is the strategy's whole proposition:
    a reader asked for the document's own boundaries, and gluing three short sections into
    one chunk to hit a size target throws away the structure they chose it for.
    """
    pieces: list[tuple[str, str | None]] = []
    for section in extracted.sections:
        text = section.text.strip()
        if not text:
            continue
        offsets = tokenizer.offsets(text)
        if len(offsets) - 1 <= config.chunk_size:
            pieces.append((text, section.title))
            continue
        pieces.extend((part, section.title) for part in _split(text, offsets, config))
    return pieces


def _whole_document(
    extracted: Extracted, config: ChunkingConfig, tokenizer: Tokenizer
) -> list[tuple[str, str | None]]:
    """Split the document as one run of text, labelling each chunk with the section it
    starts in.

    The joining is what produces full-size chunks; the label is what keeps a citation
    usable. Losing the second would make ``recursive`` the strategy that indexes well and
    attributes badly, for no reason other than how the text was assembled.
    """
    text, marks = _joined(extracted.sections)
    if not text.strip():
        return []
    offsets = tokenizer.offsets(text)
    pieces: list[tuple[str, str | None]] = []
    cursor = 0
    for part in _split(text, offsets, config):
        start = text.find(part, cursor)
        if start < 0:  # only if a splitter ever rewrote its input, which none of them do
            start = cursor
        pieces.append((part, _section_at(marks, start)))
        cursor = start + 1
    return pieces


def _joined(sections: tuple[Section, ...]) -> tuple[str, list[tuple[int, str | None]]]:
    parts: list[str] = []
    marks: list[tuple[int, str | None]] = []
    position = 0
    for section in sections:
        text = section.text.strip()
        if not text:
            continue
        marks.append((position, section.title))
        parts.append(text)
        position += len(text) + 2  # the "\n\n" that joins them
    return "\n\n".join(parts), marks


def _section_at(marks: list[tuple[int, str | None]], position: int) -> str | None:
    title: str | None = None
    for start, name in marks:
        if start > position:
            break
        title = name
    return title


def _split(text: str, offsets: list[int], config: ChunkingConfig) -> list[str]:
    """The splitter both strategies above delegate to."""
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


__all__ = ["MIN_FILL", "Chunk", "chunk_document"]
