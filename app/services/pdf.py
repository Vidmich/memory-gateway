"""PDF text extraction (SPEC §9.2, task 11).

**Which library, and why it is neither of the two the plan named.** The plan offered
``pymupdf`` for speed or ``pdfplumber`` for table fidelity. Tables beyond simple row
rendering are out of scope for this task, which leaves speed — and leaves a licensing
question the plan does not raise: PyMuPDF is AGPL-3.0, whose network clause is a live
question for a hosted multi-tenant service and not one an extractor should settle on its
own. pdfplumber is MIT and answers that, but it is built on pdfminer, a pure-Python PDF
interpreter: measured here on a 200-page, 8400-line document it takes **25 seconds**, and
nineteen of those are spent before any layout work begins.

``pypdfium2`` is the third option. It is a thin binding over PDFium — the engine in
Chrome's PDF viewer, BSD-3-Clause — and it reads that same document in **0.9 seconds**. It
returns text one rectangle at a time with coordinates, which is what makes header and
footer detection *positional* rather than a guess about line numbers, and it exposes the
outline and the encryption state. So the trade the plan framed as speed-versus-tables is
really speed-versus-licence, and there was an option that gives up neither.

**What the rest of this module is for.** Getting characters out of a PDF is the easy half.
The hard half is that a PDF has no paragraphs, no reading order, and no idea which of its
text is content — so four things are undone here before anything reaches the index.

*Running headers and footers.* "ACME Corporation — Confidential" on all 200 pages ends up
in all 200 chunks, and every chunk is then partly about ACME's confidentiality notice.
Similarity scores compress toward each other and retrieval stops discriminating. Detected
by position and repetition together: a line in the top or bottom band of the page whose
text — with digit runs masked, so ``Page 4 of 200`` and ``Page 5 of 200`` are the same
line — recurs at the same height on most pages.

*Hyphenation.* A word broken across a line break is two tokens that mean nothing, and
``manufac-`` is what a citation would show a reader.

*Columns.* PDFium returns text in the order the page draws it, which for a two-column
layout interleaves the columns line by line. Every sentence is then cut in half by the
sentence beside it.

*Pages that are pictures.* A scan has no text layer. Indexing it produces a document that
reports ``indexed`` with a handful of near-empty chunks, which is worse than a failure
because nothing looks wrong. Below a floor of extracted characters per page the whole
document is skipped as ``needs_ocr`` — a state the UI explains rather than one somebody
has to infer.

The unit is the **page**, always: one :class:`~app.services.extraction.Section` per page,
titled ``p. 147``, or ``Warranty > Coverage (p. 147)`` where the document has an outline.
Two consequences follow from that, and both are deliberate.

The outline feeds the *label* rather than the grouping. Merging pages into chapters would
let ``by_heading`` cut on the document's own structure, but a citation that says
"somewhere in this 40-page chapter" is not a citation.

And **chunks never cross a page break**, whatever the connector's chunking strategy says —
the sections are marked atomic, and :func:`~app.services.chunking.chunk_document` honours
that. The alternative is a chunk drawn from pages 144 to 147 and labelled with the page it
happened to start on, which is a citation that is *nearly* right: an end user turns to page
144, does not find the sentence, and stops believing the citations. Being coarse is
survivable and being subtly wrong is not. The cost is real and worth naming — a page holds
less than a full token window, so a manual produces more and slightly smaller chunks than
it would if the text were run together.
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.extraction import Extracted, ExtractionError, Section, SkippedDocument

logger = logging.getLogger(__name__)

#: Below this many extracted characters per page, averaged over the document, there is no
#: text layer worth indexing. Averaged rather than tested per page: a manual with a dozen
#: full-page diagrams in it is still a text document, and skipping it would be wrong.
MIN_CHARS_PER_PAGE = 100

#: Pages past this are not read. A 50 MB PDF can hold tens of thousands of pages, and the
#: memory ceiling has to come from somewhere other than hope. Mirrors the CSV row cap:
#: truncate, and say so in the log.
MAX_PAGES = 2000

#: Fraction of the page height counted as the running-header and running-footer bands.
BAND = 0.12

#: How much of the document a repeated line must appear on before it is boilerplate. Below
#: this it is more likely a heading style that happens to recur.
REPEAT_RATIO = 0.6

#: Repetition means nothing on a two-page document — every line of a cover page would
#: qualify — so boilerplate stripping does not run at all below this.
MIN_PAGES_FOR_BOILERPLATE = 3

#: Points of vertical wobble allowed before two lines count as being at different heights.
#: Nobody places a running header to the tenth of a point.
BAND_TOLERANCE = 4.0

#: A gutter narrower than this fraction of the text area is word spacing, not a column
#: break.
MIN_GUTTER = 0.03

#: Column detection needs enough lines to be evidence rather than coincidence.
MIN_LINES_FOR_COLUMNS = 8

#: Resolution of the projection profile. A hundred buckets across the text area puts each
#: one at about six points, which is finer than any gutter worth finding.
BUCKETS = 100

#: How many lines may cross a bucket and still leave it counting as empty. A title, a
#: footer or a table lying across the channel is the normal case, and requiring a
#: genuinely empty column would find a gutter on almost no real page.
GUTTER_NOISE = 0.15

#: Where a gutter is allowed to be. A wide left margin is not a gutter, and neither is the
#: ragged right edge of a single column of prose.
SEARCH_FROM = 0.25
SEARCH_TO = 0.75

_DIGITS = re.compile(r"\d+")
_WHITESPACE = re.compile(r"\s+")

#: PDFium's own marker for a hyphen it believes was inserted to break a word across a
#: line. It is a better signal than any heuristic — the engine has the glyph positions and
#: the font's own hyphenation state — so where it appears it is taken at its word. It must
#: also never reach the index: it is a control character, and it would be embedded.
SOFT_BREAK = "\x02"

#: Everything else below U+0020. PDFium emits these for glyphs with no Unicode mapping,
#: and a chunk carrying them is a chunk that renders as boxes in the request drawer.
_CONTROLS = re.compile(r"[\x00-\x01\x03-\x1f]")
#: ASCII hyphen, soft hyphen, Unicode hyphen, non-breaking hyphen. Escaped rather than
#: written literally: three of the four are indistinguishable in a source file, which is
#: exactly the kind of thing that gets "fixed" by a well-meaning search and replace.
_HYPHENS = "-\u00ad\u2010\u2011"


@dataclass(frozen=True, slots=True)
class Line:
    """One run of text with its place on the page.

    ``top`` is distance from the top of the page, not PDF user space, which measures from
    the bottom. Converting once, here, means every comparison below reads the way a person
    looks at a page.
    """

    text: str
    x0: float
    x1: float
    top: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0


def extract_pdf(data: bytes, *, name: str) -> Extracted:
    """The registered extractor: one section per page, boilerplate removed."""
    document = _open(data, name=name)
    try:
        pages = _read(document)
        headings = _headings(document, len(pages))
    finally:
        document.close()

    if not pages:
        # A PDF with no pages is not a failure; it is a document with nothing in it, and
        # the pipeline already has a sentence for that.
        return Extracted(sections=(), page_count=0)

    boilerplate = _boilerplate(pages)
    rendered = [(number, _render(lines, boilerplate)) for number, lines in pages]
    if sum(len(text) for _, text in rendered) < MIN_CHARS_PER_PAGE * len(rendered):
        plural = "" if len(rendered) == 1 else "s"
        raise SkippedDocument(
            f"This PDF has {len(rendered)} page{plural} and almost no text in it, so it "
            "is probably a scan. Optical character recognition is not available yet — "
            "upload a version with a text layer.",
            reason="needs_ocr",
        )

    sections = tuple(
        Section(text=text, title=_title(headings.get(number - 1), number))
        for number, text in rendered
        if text
    )
    return Extracted(sections=sections, page_count=len(pages), atomic_sections=True)


# ---------------------------------------------------------------------------
# opening
# ---------------------------------------------------------------------------


def _open(data: bytes, *, name: str) -> Any:
    """Open the document, distinguishing "locked" from "broken".

    The empty password is tried because it is the one that works. A PDF carrying only an
    *owner* password — the permissions kind, "you may read this but not print it" — is
    encrypted and opens with no password at all, and refusing those would skip a large
    share of the corporate documents anybody actually has.
    """
    import pypdfium2 as pdfium

    try:
        return pdfium.PdfDocument(io.BytesIO(data), password="")
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise ExtractionError(
                "This PDF is password-protected. Save an unprotected copy and upload "
                "that; the gateway does not store document passwords.",
                reason="password_protected",
            ) from exc
        logger.info("pdf would not open", extra={"file": name, "error": str(exc)})
        raise ExtractionError(
            f"This PDF could not be opened: {exc} It may be truncated or corrupt.",
            reason="malformed_pdf",
        ) from exc


def _read(document: Any) -> list[tuple[int, list[Line]]]:
    """Every page's lines, in the order PDFium draws them."""
    count = len(document)
    if count > MAX_PAGES:
        logger.info("pdf truncated at the page cap", extra={"pages": count, "cap": MAX_PAGES})
        count = MAX_PAGES

    pages: list[tuple[int, list[Line]]] = []
    for index in range(count):
        page = document[index]
        try:
            height = float(page.get_size()[1])
            text = page.get_textpage()
            try:
                pages.append((index + 1, list(_lines(text, height))))
            finally:
                text.close()
        finally:
            page.close()
    return pages


def _lines(text: Any, height: float) -> Iterator[Line]:
    for index in range(text.count_rects()):
        rect = text.get_rect(index)
        content = _clean(text.get_text_bounded(*rect))
        if not content:
            continue
        left, _, right, top = rect
        yield Line(text=content, x0=float(left), x1=float(right), top=height - float(top))


def _clean(raw: str) -> str:
    return _WHITESPACE.sub(" ", _CONTROLS.sub("", raw)).strip()


# ---------------------------------------------------------------------------
# running headers and footers
# ---------------------------------------------------------------------------


def _boilerplate(pages: Sequence[tuple[int, list[Line]]]) -> frozenset[tuple[int, str]]:
    """The keys of lines that repeat at the same height, in a band, on most pages.

    Position and repetition are both required. Position alone would strip a heading that
    happens to sit near the top of a page; repetition alone would strip a sentence a
    reference document genuinely repeats in its body.
    """
    if len(pages) < MIN_PAGES_FOR_BOILERPLATE:
        return frozenset()

    seen: dict[tuple[int, str], set[int]] = {}
    for number, lines in pages:
        height = _height(lines)
        for line in lines:
            key = _key(line, height)
            if key is not None:
                seen.setdefault(key, set()).add(number)

    floor = REPEAT_RATIO * len(pages)
    return frozenset(key for key, on in seen.items() if len(on) >= floor)


def _height(lines: Sequence[Line]) -> float:
    """The page height, recovered from the lines themselves.

    Kept off :class:`Line` because the height is a fact about the page, and putting it on
    every line would invite two lines of one page to disagree about it. The lowest line's
    baseline is close enough: the bands are a tenth of the page, and the error is a
    margin.
    """
    return max((line.top for line in lines), default=0.0)


def _key(line: Line, page_height: float) -> tuple[int, str] | None:
    """A boilerplate identity, or ``None`` for a line in the body of the page."""
    if page_height <= 0:
        return None
    band = BAND * page_height
    if band < line.top < page_height - band:
        return None
    # Digit runs are masked so "Page 4 of 200" and "Page 5 of 200" are recognised as the
    # same running footer. Without it, the most common piece of boilerplate in existence
    # is the one piece that never repeats.
    return (round(line.top / BAND_TOLERANCE), _DIGITS.sub("#", line.text.casefold()))


# ---------------------------------------------------------------------------
# one page's text
# ---------------------------------------------------------------------------


def _render(lines: Sequence[Line], boilerplate: frozenset[tuple[int, str]]) -> str:
    height = _height(lines)
    kept = [line for line in lines if _key(line, height) not in boilerplate]
    return _dehyphenate(line.text for line in _ordered(kept))


def _ordered(lines: Sequence[Line]) -> list[Line]:
    """Reading order, columns respected.

    The page is cut into horizontal bands by any line that spans the gutter — a title, a
    full-width paragraph, a rule of text between two columned regions. Within a band the
    left column is read before the right. A page with no gutter is one band, which is the
    ordinary single-column case and costs one sort.
    """
    ranked = sorted(lines, key=lambda line: (line.top, line.x0))
    gutter = _gutter(ranked)
    if gutter is None:
        return ranked

    ordered: list[Line] = []
    band: list[Line] = []

    def flush() -> None:
        ordered.extend(line for line in band if line.x1 <= gutter)
        ordered.extend(line for line in band if line.x1 > gutter)
        band.clear()

    for line in ranked:
        if line.x0 < gutter < line.x1:
            flush()
            ordered.append(line)
        else:
            band.append(line)
    flush()
    return ordered


def _gutter(lines: Sequence[Line]) -> float | None:
    """The x of a vertical channel the columns leave between them, or ``None``.

    A projection profile: the text area is divided into buckets, every line adds one to
    each bucket it covers, and a gutter is the widest run of near-empty buckets in the
    middle of the page. Near-empty rather than empty is the whole reason it is a profile
    and not a gap scan — a two-column page almost always has a title, a footer or a table
    lying across the channel, and one of those lines is enough to close a gap entirely.

    Both sides then have to hold enough lines to be a column rather than a stray caption,
    which is what stops a page with one indented block from being read as two columns.
    """
    if len(lines) < MIN_LINES_FOR_COLUMNS:
        return None
    left = min(line.x0 for line in lines)
    right = max(line.x1 for line in lines)
    span = right - left
    if span <= 0:
        return None

    width = span / BUCKETS
    covered = [0] * BUCKETS
    for line in lines:
        start = max(0, int((line.x0 - left) / width))
        end = min(BUCKETS - 1, int((line.x1 - left) / width))
        for bucket in range(start, end + 1):
            covered[bucket] += 1

    noise = GUTTER_NOISE * len(lines)
    best = (0, 0)  # (length, end bucket)
    run = 0
    # Only the middle of the page: a wide left margin is not a gutter, and neither is the
    # ragged right edge of a single column of prose.
    for bucket in range(int(BUCKETS * SEARCH_FROM), int(BUCKETS * SEARCH_TO)):
        run = run + 1 if covered[bucket] <= noise else 0
        if run > best[0]:
            best = (run, bucket)
    if best[0] * width < MIN_GUTTER * span:
        return None

    gutter = left + (best[1] - best[0] / 2 + 0.5) * width
    before = sum(1 for line in lines if line.x1 <= gutter)
    after = sum(1 for line in lines if line.x0 >= gutter)
    enough = MIN_LINES_FOR_COLUMNS // 2
    return gutter if before >= enough and after >= enough else None


def _dehyphenate(lines: Iterable[str]) -> str:
    """Join a word split across a line break, and leave every other hyphen alone.

    Two rules, and the first is the one that fires in practice. PDFium marks a hyphen it
    believes was inserted to break a word with :data:`SOFT_BREAK`, and that judgement —
    made with the glyph positions and the font in hand — beats anything guessable from the
    characters, so it is joined unconditionally.

    The second is the fallback for a producer PDFium does not recognise: a trailing
    hyphen, a line with something before it, and a following line that starts lowercase.
    Narrow on purpose — "state-" followed by "of-the-art" is a real hyphen and stays one,
    and a bare "-" alone on a line is a bullet, not a break.
    """
    out: list[str] = []
    for line in lines:
        previous = out[-1] if out else ""
        if previous.endswith(SOFT_BREAK):
            out[-1] = previous[: -len(SOFT_BREAK)] + line
            continue
        if len(previous) > 1 and previous[-1] in _HYPHENS and line[:1].islower():
            out[-1] = previous[:-1] + line
            continue
        out.append(line)
    # Anything left is a marker in the middle of a line, where it is an ordinary hyphen
    # that PDFium happened to label. Rendered as one rather than dropped: "re-entry" and
    # "reentry" are different words.
    return "\n".join(out).replace(SOFT_BREAK, "-").strip()


# ---------------------------------------------------------------------------
# outline
# ---------------------------------------------------------------------------


def _headings(document: Any, pages: int) -> dict[int, str]:
    """Page index to heading path, carried forward from the last bookmark that opened.

    A bookmark names where a chapter *starts*, so the pages between two bookmarks belong
    to the earlier one — which is why this is a forward fill and not a lookup.
    """
    try:
        bookmarks = list(document.get_toc())
    except Exception as exc:  # a malformed outline is not a reason to lose the text
        logger.info("pdf outline could not be read", extra={"error": str(exc)})
        return {}

    starts: dict[int, str] = {}
    path: list[str] = []
    for bookmark in bookmarks:
        destination = bookmark.get_dest()
        if destination is None:
            continue
        index = destination.get_index()
        title = _WHITESPACE.sub(" ", str(bookmark.get_title() or "")).strip()
        if index is None or not title:
            continue
        del path[bookmark.level :]
        path.append(title)
        starts[int(index)] = " > ".join(path)

    if not starts:
        return {}

    filled: dict[int, str] = {}
    current = ""
    for index in range(pages):
        current = starts.get(index, current)
        if current:
            filled[index] = current
    return filled


def _title(heading: str | None, page: int) -> str:
    """SPEC §9.3's ``page_or_section``, rendered so that a citation reads as one.

    ``manual.pdf (p. 147)`` is what an end user gets to check. The heading path goes in
    front of it where the document has one, because "Warranty > Coverage" is what tells a
    reader whether the page is worth turning to.
    """
    where = f"p. {page}"
    return f"{heading} ({where})" if heading else where


__all__ = [
    "MAX_PAGES",
    "MIN_CHARS_PER_PAGE",
    "Line",
    "extract_pdf",
]
