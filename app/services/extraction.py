"""Turning bytes into text worth embedding.

Extraction is the step where quality is won or lost, and the two decisions that matter
are both about *rendering* rather than parsing.

**CSV and TSV become records, not rows.** ``Ada,Engineer,London`` embeds to a vector
about commas. ``name: Ada / role: Engineer / office: London`` embeds to a vector about a
person, and a question about engineers in London can actually reach it. The header row is
carried into every record, which is the entire point — a value without its column name is
a string with no meaning attached.

**JSON becomes key paths.** ``{"user": {"roles": ["admin"]}}`` renders as
``user.roles.0: admin``. The alternative is embedding punctuation and indentation.

Everything else is a decode plus structure. Which structure is the second half of the
job: an extractor returns :class:`Section` objects, not a string, because SPEC §9.3's
``page_or_section`` metadata and the ``by_heading`` chunking strategy both need to know
where the document's own boundaries are. A format with no structure returns one section,
and ``by_heading`` falls back to ``recursive`` for it — which is the fallback the SPEC
asks for, arrived at by the shape of the data rather than by a special case.

PDF and Office extraction lives in :mod:`app.services.pdf` and :mod:`app.services.office`
and arrives here as four more :meth:`ExtractorRegistry.register` calls — which is what the
registry was for. Those four are also the only ones marked for **isolation**: they are
third-party parsers over adversarial binary input, where a malformed file can loop, eat a
gigabyte, or take the interpreter down with it, and none of those should be able to touch
a worker that is halfway through somebody else's upload. See
:mod:`app.services.extraction_pool`.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Protocol

from charset_normalizer import from_bytes

from app.services.filetypes import (
    DOCX,
    FORMAT_KINDS,
    PDF,
    PPTX,
    XLSX,
    bom_encoding,
    extension_of,
    is_text,
)

logger = logging.getLogger(__name__)

#: How much of a file is examined to guess its encoding. Detection is quadratic-ish in
#: the sample and the answer does not improve past a few kilobytes of real text.
DETECT_BYTES = 32 * 1024

#: A cell value longer than this is truncated in the rendered record. Tabular exports
#: routinely carry a whole document in one cell, and one row becoming ten chunks makes
#: the other columns unreachable.
MAX_CELL_CHARS = 2000

#: CSV rows are rendered one per record. Past this the file is data, not prose, and
#: rendering all of it would produce tens of thousands of near-identical chunks that
#: crowd out everything else in the index.
MAX_CSV_ROWS = 20_000


class ExtractionError(Exception):
    """The file could not be read. The message goes to ``documents.error`` verbatim, so
    it is written for a customer: what was wrong, and where.

    ``reason`` is the same fact in a form a program can branch on. The sentence is for a
    person and will be rewritten as the wording improves; the code is what the UI matches
    on to turn "password-protected" into an explained state with a way out of it, rather
    than a red row with a paragraph in it. Deliberately not constrained by the database —
    a new extractor should not need a migration to explain itself.
    """

    def __init__(self, message: str, *, reason: str = "extraction_failed") -> None:
        super().__init__(message)
        self.reason = reason

    def __reduce__(self) -> tuple[Any, ...]:
        """Survive a trip through :mod:`app.services.extraction_pool`.

        An exception raised in a subprocess is pickled back to the parent, and the default
        reconstruction calls ``cls(*args)`` — which drops a keyword-only field. Without
        this, every ``needs_ocr`` decided inside an isolated extractor would arrive as a
        generic failure, and the UI would show a red row instead of the explanation. The
        symptom would appear only in the isolated formats, which are exactly the ones that
        needed the reasons.
        """
        return (_rebuild_error, (type(self), str(self), self.reason))


class SkippedDocument(ExtractionError):
    """Read successfully, and deliberately not indexed.

    The distinction from a plain failure is the one the customer cares about. A failure is
    something that went wrong and might work on retry; this is a decision, and pressing
    **Retry** on it will reach the same decision again. A scan with no text layer is the
    case this exists for: indexing it would produce a document that reports ``indexed``
    with nothing in it, which is worse than a refusal because nothing looks wrong.
    """


def _rebuild_error(kind: type[ExtractionError], message: str, reason: str) -> ExtractionError:
    """Named by :meth:`ExtractionError.__reduce__`; module-level so that it pickles."""
    return kind(message, reason=reason)


@dataclass(frozen=True, slots=True)
class Section:
    """A run of text with a name — SPEC §9.3's ``page_or_section``.

    The title is a *path* for nested structure (``Setup > Installing > Windows``), not
    just the nearest heading. A retrieved chunk labelled "Windows" tells a reader
    nothing; the path tells them where in the document they landed.
    """

    text: str
    title: str | None = None


@dataclass(frozen=True, slots=True)
class Extracted:
    sections: tuple[Section, ...]
    #: Pages, slides or sheets — the format's own unit, named in the UI from the media
    #: type. ``None`` where the format has no such unit: a Word document's pagination is
    #: decided by the renderer from the fonts and the paper size, so any number here would
    #: be invented.
    page_count: int | None = None
    #: Whether the chunker must keep these boundaries whatever the connector's strategy
    #: says. Two formats claim it, for two different reasons: a slide is a unit somebody
    #: authored, and gluing two into one chunk produces a chunk about two subjects; a PDF
    #: page is what a citation names, and a chunk spanning four of them can only cite one
    #: of the four truthfully. The extractor sets it because the extractor is the only
    #: thing that knows which format it is looking at — see
    #: :func:`~app.services.chunking.chunk_document`.
    atomic_sections: bool = False

    @property
    def text(self) -> str:
        return "\n\n".join(section.text for section in self.sections if section.text)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


class Extractor(Protocol):
    def __call__(self, data: bytes, *, name: str) -> Extracted: ...


# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------


def decode(data: bytes, *, name: str = "") -> str:
    """Bytes to text, in the order that gets the answer right most often.

    A byte-order mark is a declaration, so it wins outright. Otherwise UTF-8 is tried
    first — it is what the overwhelming majority of files actually are, and detection on
    a file that is already valid UTF-8 can still talk itself into a legacy codepage on a
    short sample. Only when UTF-8 fails does statistical detection get a turn, and only
    then is a legacy encoding possible at all.

    A file that survives none of that fails with the byte offset that broke it. "Could
    not decode" alone sends somebody looking through a 200 MB export by hand.
    """
    if not data:
        return ""

    declared = bom_encoding(data)
    if declared is not None:
        try:
            # `utf-16-le` and friends are the *specific* codecs, which do not consume the
            # mark the way bare `utf-16` does — so it survives as U+FEFF at position zero
            # and rides into the first chunk of every UTF-16 document.
            return data.decode(declared).lstrip("﻿")
        except UnicodeDecodeError as exc:
            raise ExtractionError(_decode_message(exc, declared, name)) from exc

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Bound outside the handler: Python deletes the `as` name when the block ends,
        # and this is the failure reported if detection below also comes up empty.
        utf8_error = exc

    best = from_bytes(data[:DETECT_BYTES]).best()
    if best is not None:
        encoding = str(best.encoding)
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            logger.info(
                "detected encoding did not decode the whole file",
                extra={"file": name, "encoding": encoding},
            )

    raise ExtractionError(_decode_message(utf8_error, "utf-8", name))


def _decode_message(exc: UnicodeDecodeError, encoding: str, name: str) -> str:
    where = f"byte {exc.start}"
    return (
        f"This file is not valid {encoding} text and its encoding could not be detected "
        f"({where}). Re-save it as UTF-8 and upload it again."
    )


# ---------------------------------------------------------------------------
# extractors
# ---------------------------------------------------------------------------


def extract_plain(data: bytes, *, name: str) -> Extracted:
    """Direct decode, one section. Source code, logs, RST, XML, YAML.

    XML is deliberately *not* tag-stripped. Its tags carry the meaning — an element name
    is the only label a value has — and stripping them leaves a column of bare strings.
    """
    return Extracted(sections=(Section(text=decode(data, name=name).strip()),))


_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def extract_markdown(data: bytes, *, name: str) -> Extracted:
    """Split on ATX headings, keeping the heading path as each section's title.

    Fenced code blocks are tracked, because ``# comment`` as the first line of a shell
    snippet is not a heading, and treating it as one splits a code block in half.
    """
    text = decode(data, name=name)
    sections: list[Section] = []
    path: list[str] = []
    body: list[str] = []
    fence: str | None = None

    def flush() -> None:
        content = "\n".join(body).strip()
        if content:
            sections.append(Section(text=content, title=" > ".join(path) or None))
        body.clear()

    for line in text.splitlines():
        opening = _FENCE.match(line)
        if opening:
            marker = opening.group(1)
            fence = None if fence == marker else (fence or marker)
            body.append(line)
            continue
        if fence is not None:
            body.append(line)
            continue

        heading = _HEADING.match(line)
        if heading is None:
            body.append(line)
            continue

        flush()
        level = len(heading.group(1))
        del path[level - 1 :]
        path.append(heading.group(2))
        # The heading stays in the body as well as in the title: it is the most
        # information-dense line in the section, and a chunk that drops it loses the
        # only sentence that names what the section is about.
        body.append(line)

    flush()
    return Extracted(sections=tuple(sections) or (Section(text=text.strip()),))


class _HtmlText(HTMLParser):
    """HTML to text, keeping headings and dropping everything that is not content."""

    #: Tags whose *contents* are not text a reader would ever see.
    SKIP = frozenset({"script", "style", "noscript", "template", "svg", "head"})
    #: Tags that end a line. Everything else is inline and must not gain whitespace, or
    #: ``<b>data</b>base`` becomes two words.
    BLOCK = frozenset(
        {
            "p",
            "div",
            "section",
            "article",
            "header",
            "footer",
            "main",
            "aside",
            "li",
            "tr",
            "table",
            "ul",
            "ol",
            "dl",
            "dt",
            "dd",
            "pre",
            "blockquote",
            "form",
            "figure",
            "figcaption",
            "nav",
            "br",
            "hr",
        }
    )
    HEADINGS = ("h1", "h2", "h3", "h4")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[Section] = []
        self._path: list[str] = []
        self._body: list[str] = []
        self._skip_depth = 0
        self._heading: str | None = None
        self._heading_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self.HEADINGS:
            self._flush()
            self._heading = tag
            self._heading_text = []
            return
        if tag in self.BLOCK:
            self._body.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == self._heading:
            title = _collapse("".join(self._heading_text))
            level = self.HEADINGS.index(tag) + 1
            del self._path[level - 1 :]
            if title:
                self._path.append(title)
                self._body.append(f"{title}\n")
            self._heading = None
            return
        if tag in self.BLOCK:
            self._body.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._heading is not None:
            self._heading_text.append(data)
            return
        self._body.append(data)

    def _flush(self) -> None:
        content = _tidy("".join(self._body))
        if content:
            self.sections.append(Section(text=content, title=" > ".join(self._path) or None))
        self._body = []

    def finish(self) -> tuple[Section, ...]:
        self._flush()
        return tuple(self.sections)


def extract_html(data: bytes, *, name: str) -> Extracted:
    parser = _HtmlText()
    try:
        parser.feed(decode(data, name=name))
        parser.close()
    except ExtractionError:
        raise
    except Exception as exc:
        # `html.parser` is lenient by design and almost never raises. When it does, the
        # file is not HTML at all, and saying so beats a stack trace in `documents.error`.
        raise ExtractionError(f"This file could not be parsed as HTML: {exc}") from exc
    return Extracted(sections=parser.finish() or (Section(text=""),))


def extract_csv(data: bytes, *, name: str, delimiter: str | None = None) -> Extracted:
    """One record per row, rendered ``column: value``.

    The delimiter is sniffed rather than taken from the extension, because half the
    world's ``.csv`` files are semicolon-separated exports from a spreadsheet in a locale
    that uses the comma for decimals. ``.tsv`` passes one explicitly: the format names its
    delimiter, and letting a sniffer overrule that is how a tab-separated file with commas
    inside its cells silently becomes one column.
    """
    text = decode(data, name=name)
    if not text.strip():
        return Extracted(sections=(Section(text=""),))

    separator = delimiter or _sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=separator)
    try:
        header = next(reader, None)
    except csv.Error as exc:
        raise ExtractionError(f"This file could not be read as delimited text: {exc}") from exc
    if header is None:
        return Extracted(sections=(Section(text=""),))

    columns = [cell.strip() or f"column {index + 1}" for index, cell in enumerate(header)]
    records: list[str] = []
    truncated = False
    try:
        for number, row in enumerate(reader, start=1):
            if number > MAX_CSV_ROWS:
                truncated = True
                break
            rendered = render_record(columns, row)
            if rendered:
                records.append(rendered)
    except csv.Error as exc:
        raise ExtractionError(f"This file could not be read as delimited text: {exc}") from exc

    if truncated:
        logger.info("csv truncated at the row cap", extra={"file": name, "max_rows": MAX_CSV_ROWS})
    # One section per record would put a heading boundary between every two rows, which
    # is not what `by_heading` is for; the blank line between records is boundary enough
    # for the recursive splitter to cut cleanly.
    return Extracted(sections=(Section(text="\n\n".join(records)),))


def extract_tsv(data: bytes, *, name: str) -> Extracted:
    return extract_csv(data, name=name, delimiter="\t")


def _sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:20])
    try:
        return str(csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter)
    except csv.Error:
        return ","


def render_record(columns: Sequence[str], row: Sequence[str]) -> str:
    lines = []
    for index, value in enumerate(row):
        cleaned = value.strip()
        if not cleaned:
            # An empty cell is not information. Rendering "office:" for every row would
            # add a token per column per row and say nothing.
            continue
        column = columns[index] if index < len(columns) else f"column {index + 1}"
        lines.append(f"{column}: {_clip(cleaned)}")
    return "\n".join(lines)


def extract_json(data: bytes, *, name: str) -> Extracted:
    text = decode(data, name=name)
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionError(
            f"This file is not valid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}."
        ) from exc
    return Extracted(sections=(Section(text="\n".join(flatten(document))),))


def extract_jsonl(data: bytes, *, name: str) -> Extracted:
    """One record per line. A bad line names its own line number and is skipped.

    Failing the whole file for one malformed row is the wrong trade for this format:
    JSONL is what append-only exporters produce, and a truncated final line is the single
    most common thing wrong with one.
    """
    text = decode(data, name=name)
    records: list[str] = []
    bad = 0
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            records.append("\n".join(flatten(json.loads(stripped))))
        except json.JSONDecodeError:
            bad += 1
            logger.info("skipping malformed jsonl line", extra={"file": name, "line": number})

    if bad and not records:
        raise ExtractionError(
            f"None of the {bad} lines in this file could be read as JSON. "
            "JSON Lines expects one complete JSON value per line."
        )
    return Extracted(sections=(Section(text="\n\n".join(records)),))


def flatten(value: Any, prefix: str = "") -> Iterator[str]:
    """``{"a": {"b": [1]}}`` becomes ``a.b.0: 1``.

    Scalars render as ``path: value``; an empty object or list renders as nothing, since
    ``settings: {}`` is a fact about the exporter rather than about the subject.
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from flatten(item, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from flatten(item, f"{prefix}.{index}" if prefix else str(index))
    elif value is None:
        return
    else:
        rendered = _clip(str(value).strip())
        if rendered:
            yield f"{prefix}: {rendered}" if prefix else rendered


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


#: Task 104. Which version of each format's extractor this build ships, keyed by
#: :data:`~app.services.filetypes.FORMAT_KINDS`. Part of every document's index
#: fingerprint, because an extractor that produces different text produces different
#: chunks and nothing else would say so. **Bump the number for a format when its
#: extractor's output changes** — a PDF layout fix, a new table renderer — and every
#: document of that format reads *stale: extractor upgraded* on the next deploy, while
#: the formats whose extractor did not change stay current.
EXTRACTION_VERSIONS: dict[str, int] = dict.fromkeys(FORMAT_KINDS, 1)


def extraction_version(kind: str) -> int:
    return EXTRACTION_VERSIONS.get(kind, 1)


@dataclass(frozen=True, slots=True)
class Registration:
    """One extractor, plus how it is allowed to run."""

    extractor: Extractor
    #: The name a subprocess can look this extractor up under, or ``None`` for one that
    #: runs in the worker like everything else. A *name* rather than the function itself
    #: because the function has to cross a process boundary and a closure does not pickle;
    #: the child resolves it from its own copy of the registry. See
    #: :mod:`app.services.extraction_pool`.
    isolation_key: str | None = None


class ExtractorRegistry:
    """Which extractor reads which file, and where it is allowed to run.

    Keyed by sniffed media type *and* by extension, and consulted in that order. The
    media type is the stronger key because it was derived from the bytes; the extension
    is the fallback for a *textual* type this build has no opinion about, which is what
    lets a ``.rst`` file work even if a future sniffer stops recognising it. The
    restriction to text is load-bearing: see :func:`~app.services.filetypes.is_text`.
    """

    def __init__(self) -> None:
        self._by_media_type: dict[str, Registration] = {}
        self._by_extension: dict[str, Registration] = {}
        self._pending: dict[str, str] = {}

    def register(
        self,
        extractor: Extractor,
        *,
        media_types: Iterable[str] = (),
        extensions: Iterable[str] = (),
        isolation_key: str | None = None,
    ) -> None:
        registration = Registration(extractor=extractor, isolation_key=isolation_key)
        for media_type in media_types:
            self._by_media_type[media_type] = registration
            self._pending.pop(media_type, None)
        for extension in extensions:
            self._by_extension[extension.lower()] = registration

    def register_coming_soon(self, media_type: str, note: str) -> None:
        """Recognised, deliberately not implemented yet.

        The distinction between this and "unknown" is the whole reason the method exists:
        one is a roadmap item and the other is a file nobody should have uploaded, and a
        customer needs to be able to tell which they are looking at.
        """
        if media_type not in self._by_media_type:
            self._pending[media_type] = note

    def lookup(self, *, media_type: str, name: str) -> Registration | None:
        found = self._by_media_type.get(media_type)
        if found is not None:
            return found
        if not is_text(media_type):
            # The extension gets no say over bytes that are not text. A JPEG called
            # `notes.txt` would otherwise reach the plain-text extractor, decode to
            # mojibake, and be indexed — the exact failure sniffing exists to prevent.
            # It is also why the binary formats register no extensions at all: a `.pdf`
            # that is not a PDF must not reach a PDF parser.
            return None
        return self._by_extension.get(extension_of(name))

    def find(self, *, media_type: str, name: str) -> Extractor | None:
        found = self.lookup(media_type=media_type, name=name)
        return found.extractor if found is not None else None

    def by_isolation_key(self, key: str) -> Extractor | None:
        """The extractor a subprocess was asked to run.

        The other half of :attr:`Registration.isolation_key`: the parent sends a name, the
        child builds its own registry and resolves it here. Nothing but a string crosses
        the boundary, so a worker cannot be asked to call an arbitrary callable by anything
        that can write to the queue.
        """
        for registration in self._by_media_type.values():
            if registration.isolation_key == key:
                return registration.extractor
        return None

    def pending_note(self, media_type: str) -> str | None:
        return self._pending.get(media_type)

    def media_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_media_type))

    def extensions(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_extension))


def build_registry() -> ExtractorRegistry:
    """The formats SPEC §9.2 puts in v1."""
    registry = ExtractorRegistry()

    registry.register(
        extract_plain,
        media_types=(
            "text/plain",
            "text/x-rst",
            "application/yaml",
            "application/xml",
            "text/xml",
            "application/toml",
            "text/x-python",
            "text/javascript",
            "text/x-typescript",
            "text/x-tsx",
            "text/x-jsx",
            "text/x-go",
            "text/x-java",
            "text/x-ruby",
            "text/x-rust",
            "application/sql",
            "application/x-sh",
            "text/x-c",
            "text/x-c++",
            "text/x-csharp",
            "text/x-php",
            "text/x-kotlin",
            "text/x-swift",
        ),
        extensions=(".txt", ".rst", ".yaml", ".yml", ".xml", ".toml", ".log"),
    )
    registry.register(extract_markdown, media_types=("text/markdown",), extensions=(".md",))
    registry.register(extract_html, media_types=("text/html",), extensions=(".html", ".htm"))
    registry.register(extract_csv, media_types=("text/csv",), extensions=(".csv",))
    registry.register(extract_tsv, media_types=("text/tab-separated-values",), extensions=(".tsv",))
    registry.register(extract_json, media_types=("application/json",), extensions=(".json",))
    registry.register(
        extract_jsonl, media_types=("application/x-ndjson",), extensions=(".jsonl", ".ndjson")
    )

    # Task 11. Imported here rather than at module scope because both modules import from
    # this one; a function body runs after this module is fully loaded, so the cycle never
    # forms. Media types only, no extensions: these are binary formats, the bytes have
    # already settled what they are, and letting a name overrule that would hand a renamed
    # executable to a parser written in C.
    from app.services.office import extract_docx, extract_pptx, extract_xlsx
    from app.services.pdf import extract_pdf

    registry.register(extract_pdf, media_types=(PDF,), isolation_key="pdf")
    registry.register(extract_docx, media_types=(DOCX,), isolation_key="docx")
    registry.register(extract_pptx, media_types=(PPTX,), isolation_key="pptx")
    registry.register(extract_xlsx, media_types=(XLSX,), isolation_key="xlsx")

    # EPUB is sniffed as its own type — it is a ZIP with a known extension — but nothing
    # reads it yet. Recognised and deferred, rather than lumped in with the videos: one is
    # a roadmap item and the other is a file nobody should have uploaded, and a customer
    # needs to be able to tell which they are looking at.
    registry.register_coming_soon("application/epub+zip", "EPUB extraction is not available yet")
    return registry


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def _collapse(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def _tidy(value: str) -> str:
    lines = (_collapse(line) for line in value.splitlines())
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _clip(value: str) -> str:
    return value if len(value) <= MAX_CELL_CHARS else f"{value[:MAX_CELL_CHARS]}…"


__all__ = [
    "EXTRACTION_VERSIONS",
    "Extracted",
    "ExtractionError",
    "Extractor",
    "ExtractorRegistry",
    "Registration",
    "Section",
    "SkippedDocument",
    "build_registry",
    "decode",
    "extract_csv",
    "extract_html",
    "extract_json",
    "extract_jsonl",
    "extract_markdown",
    "extract_plain",
    "extract_tsv",
    "extraction_version",
    "flatten",
    "render_record",
]
