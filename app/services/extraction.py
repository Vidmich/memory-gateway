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

Task 11 adds PDF and Office extractors by registering them. Until then those extensions
are *recognised* and skipped with "coming soon" rather than failed: a customer who drags
in a folder of PDFs should learn that the feature is not here yet, not conclude that the
product is broken.
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

from app.services.filetypes import bom_encoding, extension_of, is_text

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
    it is written for a customer: what was wrong, and where."""


class UnsupportedFormat(ExtractionError):
    """Recognised, but nothing here can read it. Becomes ``skipped``, not ``failed``."""


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
            rendered = _record(columns, row)
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


def _record(columns: Sequence[str], row: Sequence[str]) -> str:
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


@dataclass(frozen=True, slots=True)
class Registration:
    extractor: Extractor
    media_types: tuple[str, ...]
    extensions: tuple[str, ...]


class ExtractorRegistry:
    """Which extractor reads which file.

    Keyed by sniffed media type *and* by extension, and consulted in that order. The
    media type is the stronger key because it was derived from the bytes; the extension
    is the fallback for a *textual* type this build has no opinion about, which is what
    lets a ``.rst`` file work even if a future sniffer stops recognising it. The
    restriction to text is load-bearing: see :func:`~app.services.filetypes.is_text`.

    Task 11 calls :meth:`register` with a PDF extractor and the ``coming soon`` entry for
    ``application/pdf`` disappears — one call, no change to the pipeline.
    """

    def __init__(self) -> None:
        self._by_media_type: dict[str, Extractor] = {}
        self._by_extension: dict[str, Extractor] = {}
        self._pending: dict[str, str] = {}

    def register(
        self,
        extractor: Extractor,
        *,
        media_types: Iterable[str] = (),
        extensions: Iterable[str] = (),
    ) -> None:
        for media_type in media_types:
            self._by_media_type[media_type] = extractor
            self._pending.pop(media_type, None)
        for extension in extensions:
            self._by_extension[extension.lower()] = extractor

    def register_coming_soon(self, media_type: str, note: str) -> None:
        """Recognised, deliberately not implemented yet.

        The distinction between this and "unknown" is the whole reason the method exists:
        one is a roadmap item and the other is a file nobody should have uploaded, and a
        customer needs to be able to tell which they are looking at.
        """
        if media_type not in self._by_media_type:
            self._pending[media_type] = note

    def find(self, *, media_type: str, name: str) -> Extractor | None:
        found = self._by_media_type.get(media_type)
        if found is not None:
            return found
        if not is_text(media_type):
            # The extension gets no say over bytes that are not text. A JPEG called
            # `notes.txt` would otherwise reach the plain-text extractor, decode to
            # mojibake, and be indexed — the exact failure sniffing exists to prevent.
            return None
        return self._by_extension.get(extension_of(name))

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

    # Task 11. Named individually rather than as one "documents" bucket, because the
    # message a customer reads should be about the file they actually dropped.
    registry.register_coming_soon("application/pdf", "PDF extraction arrives in a later release")
    for media_type in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ):
        registry.register_coming_soon(
            media_type, "Office document extraction arrives in a later release"
        )
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
    "Extracted",
    "ExtractionError",
    "Extractor",
    "ExtractorRegistry",
    "Section",
    "UnsupportedFormat",
    "build_registry",
    "decode",
    "extract_csv",
    "extract_html",
    "extract_json",
    "extract_jsonl",
    "extract_markdown",
    "extract_plain",
    "extract_tsv",
    "flatten",
]
