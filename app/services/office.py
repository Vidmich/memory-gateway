"""Word, PowerPoint and Excel extraction (SPEC §9.2, task 11).

Three formats, one idea: **an Office file already knows its own structure, and the job is
to not lose it.** Each of the three has a natural unit — a heading path, a slide, a sheet —
and each of them becomes a :class:`~app.services.extraction.Section` whose title is what a
citation will show. What is left is the handful of places where the obvious call is wrong.

**Word: the accepted text, not the marked-up text.** ``python-docx`` reads a paragraph by
walking the runs directly beneath it, which quietly means an edit made with track changes
on is *dropped* — an inserted run lives inside ``w:ins`` and is not a direct child. A
policy document that has been through review would index as its pre-review draft, and
nothing about the result would look wrong. So the text is read from the XML with
insertions kept and deletions discarded, which is what "the current text of this document"
means to everybody except the parser.

**Word has no pages.** Pagination is decided by the renderer from the fonts and the paper
size, so a page number here would be invented. The heading path is the section label
instead, and the page count is null rather than a guess.

**PowerPoint: the notes are the document.** A slide reads "Q3 priorities" over three
bullets of four words each; the sentence that says what was actually decided is in the
speaker notes. Skipping them is the difference between a deck that answers questions and a
deck that indexes its own table of contents. They are labelled where they are included,
because "Notes:" is what tells a reader the model is quoting the presenter rather than the
screen.

**PowerPoint slides do not join.** A slide is a unit somebody authored; gluing two of them
into one chunk produces a chunk about two subjects, which is the failure chunking exists
to avoid. So the extractor marks its sections atomic and the chunker keeps them apart
whatever the connector's strategy is — see :func:`~app.services.chunking.chunk_document`.
A PDF page gets no such marking, because a page break is where the paper ran out.

**Excel: rows are records, not cells.** The same call task 09 made for CSV, for the same
reason — ``Ada,Engineer,London`` embeds to a vector about commas — and made here by the
same function, so the two cannot drift. A spreadsheet is usually a database export, which
is why the row cap exists: indexing 200 000 rows of it produces 200 000 near-identical
chunks that crowd everything else out of the index.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator, Sequence
from datetime import date, datetime, time
from typing import Any

from app.services.extraction import Extracted, ExtractionError, Section, render_record

logger = logging.getLogger(__name__)

#: Rows per sheet. A spreadsheet past this is a data export, and rendering all of it would
#: bury every prose document in the connector under near-identical records.
MAX_SHEET_ROWS = 50_000

#: Marks a truncated sheet in its own text, so the omission is visible to a reader of the
#: chunk rather than only to whoever reads the logs.
TRUNCATION_NOTICE = "[Truncated: only the first {rows:,} rows of this sheet are indexed.]"

#: WordprocessingML, the one namespace this module names.
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: Word paragraph styles that mean "this is a list item". Checked by prefix because the
#: built-in styles are "List Bullet", "List Number 2", and a dozen localised variants.
_LIST_STYLES = ("List Bullet", "List Number", "List Paragraph")


# ---------------------------------------------------------------------------
# Word
# ---------------------------------------------------------------------------


def extract_docx(data: bytes, *, name: str) -> Extracted:
    """One section per heading, titled with the full heading path."""
    import docx
    from docx.document import Document as DocumentType

    try:
        document: DocumentType = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise ExtractionError(
            f"This Word document could not be read: {exc}. It may be corrupt, or it may "
            "be a pre-2007 .doc file saved with a .docx name.",
            reason="malformed_docx",
        ) from exc

    footnotes = _footnotes(document)
    sections: list[Section] = []
    path: list[str] = []
    body: list[str] = []

    def flush() -> None:
        text = "\n".join(body).strip()
        if text:
            sections.append(Section(text=text, title=" > ".join(path) or None))
        body.clear()

    for block in _blocks(document, footnotes):
        if isinstance(block, str):
            body.append(block)
            continue
        level, heading = block
        flush()
        del path[level - 1 :]
        path.append(heading)
        # The heading stays in the body as well as in the title, exactly as Markdown
        # extraction keeps it: it is the most information-dense line in the section.
        body.append(heading)
    flush()

    # Word has no page count that is not a rendering decision; `None` says so.
    return Extracted(sections=tuple(sections), page_count=None)


def _blocks(document: Any, footnotes: dict[str, str]) -> Iterator[str | tuple[int, str]]:
    """Paragraphs and tables in document order, headings tagged with their level.

    Document order matters and is not what either of ``python-docx``'s two collections
    gives: ``document.paragraphs`` and ``document.tables`` are separate lists, so reading
    them in turn would move every table to the end of the file and detach it from the
    heading it belongs under.
    """
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for element in document.element.body.iterchildren():
        if element.tag == f"{W}p":
            text = _paragraph_text(element)
            if not text:
                continue
            paragraph = Paragraph(element, document)
            level = _heading_level(paragraph)
            if level is not None:
                # A heading carrying a footnote is vanishingly rare, and a heading is a
                # label: the note would make the section title unreadable.
                yield (level, text)
                continue
            text = _annotate(text, element, footnotes)
            # A list marker, rather than the run-on paragraph flattening would produce.
            yield f"- {text}" if _is_list_item(paragraph, element) else text
        elif element.tag == f"{W}tbl":
            yield from _table(Table(element, document))


def _paragraph_text(element: Any) -> str:
    """The paragraph's *current* text: insertions kept, deletions dropped.

    Every ``w:t`` beneath the element, wherever it sits — which is what picks up the runs
    inside ``w:ins`` that are not direct children of the paragraph and that
    ``Paragraph.text`` therefore never sees. Deleted text is in ``w:delText``, a different
    element, so it falls out for free; the ancestor check is for the one shape where it
    does not, an insertion nested inside a deletion, which is how Word records a move.

    Walked rather than XPathed on purpose. This is also called on the footnotes part,
    whose root is a plain lxml element with no namespace prefixes registered, and an
    XPath naming ``w:`` raises there.
    """
    parts = (node for node in element.iter(f"{W}t") if not _deleted(node))
    return "".join(node.text or "" for node in parts).strip()


def _deleted(node: Any) -> bool:
    parent = node.getparent()
    while parent is not None:
        if parent.tag == f"{W}del":
            return True
        parent = parent.getparent()
    return False


def _annotate(text: str, element: Any, footnotes: dict[str, str]) -> str:
    """Append the text of every footnote this paragraph references.

    Appended rather than inlined: a footnote spliced into the middle of a sentence breaks
    the sentence, and the sentence is what gets embedded. Kept with its paragraph rather
    than gathered into a section of its own, because a page of disembodied sentences with
    nothing saying what they are about retrieves for everything and answers nothing.
    """
    if not footnotes:
        return text
    ids = (str(node.get(f"{W}id")) for node in element.xpath(".//w:footnoteReference"))
    notes = [footnotes[identifier] for identifier in ids if identifier in footnotes]
    return f"{text} ({' '.join(notes)})" if notes else text


def _heading_level(paragraph: Any) -> int | None:
    style = getattr(paragraph.style, "name", "") or ""
    if style == "Title":
        return 1
    if style.startswith("Heading "):
        try:
            return max(1, min(9, int(style.removeprefix("Heading ").strip())))
        except ValueError:
            return None
    return None


def _is_list_item(paragraph: Any, element: Any) -> bool:
    style = getattr(paragraph.style, "name", "") or ""
    if style.startswith(_LIST_STYLES):
        return True
    # Numbering applied directly rather than through a style, which is what happens when
    # somebody presses the bullet button.
    return bool(element.xpath("./w:pPr/w:numPr"))


def _table(table: Any) -> Iterator[str]:
    """A table as records, matching the CSV convention from task 09."""
    yield from _records([[_cell_text(cell) for cell in row.cells] for row in table.rows])


def _cell_text(cell: Any) -> str:
    parts = (_paragraph_text(paragraph._p) for paragraph in cell.paragraphs)
    return " ".join(text for text in parts if text).strip()


def _records(rows: Sequence[Sequence[str]]) -> Iterator[str]:
    """A header row, then one ``column: value`` record per row.

    A one-row table is a row, not a header: rendering it as column names alone would index
    the labels and drop the content. Shared by all three formats, so the convention cannot
    come out differently depending on which one a customer uploaded.
    """
    if not rows:
        return
    header = [value or f"column {index + 1}" for index, value in enumerate(rows[0])]
    if len(rows) == 1:
        joined = " / ".join(value for value in rows[0] if value)
        if joined:
            yield joined
        return
    for row in rows[1:]:
        record = render_record(header, row)
        if record:
            yield record


def _footnotes(document: Any) -> dict[str, str]:
    """Footnote id to text, read from the footnotes part.

    Word keeps footnote bodies in a separate part and leaves a reference behind in the
    paragraph, so ignoring the part loses the text outright.
    """
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml import parse_xml

    for relationship in document.part.rels.values():
        if relationship.reltype != RT.FOOTNOTES:
            continue
        try:
            # `blob` rather than a typed part: ``python-docx`` does not model the footnotes
            # part, so what comes back is a generic ``Part`` with bytes and nothing else.
            root = parse_xml(relationship.target_part.blob)
        except Exception as exc:  # pragma: no cover - a part that will not parse
            logger.info("docx footnotes could not be read", extra={"error": str(exc)})
            return {}
        found: dict[str, str] = {}
        for note in root.iterchildren(f"{W}footnote"):
            kind = note.get(f"{W}type")
            # Word's own scaffolding: the rule drawn above the notes and its continuation.
            if kind in ("separator", "continuationSeparator", "continuationNotice"):
                continue
            text = _paragraph_text(note)
            if text:
                found[str(note.get(f"{W}id"))] = text
        return found
    return {}


# ---------------------------------------------------------------------------
# PowerPoint
# ---------------------------------------------------------------------------


def extract_pptx(data: bytes, *, name: str) -> Extracted:
    """One section per slide, titled ``Slide 3: Roadmap``."""
    from pptx import Presentation

    try:
        deck = Presentation(io.BytesIO(data))
    except Exception as exc:
        raise ExtractionError(
            f"This presentation could not be read: {exc}. It may be corrupt, or it may "
            "be a pre-2007 .ppt file saved with a .pptx name.",
            reason="malformed_pptx",
        ) from exc

    sections: list[Section] = []
    slides = list(deck.slides)
    for number, slide in enumerate(slides, start=1):
        title = _slide_title(slide)
        lines = [line for line in _shapes(slide.shapes) if line]
        notes = _notes(slide)
        if notes:
            # Labelled, so the model can tell what the audience saw from what the
            # presenter was going to say.
            lines.append(f"Notes: {notes}")
        text = "\n".join(lines).strip()
        if text:
            label = f"Slide {number}: {title}" if title else f"Slide {number}"
            sections.append(Section(text=text, title=label))

    return Extracted(sections=tuple(sections), page_count=len(slides), atomic_sections=True)


def _slide_title(slide: Any) -> str:
    holder = slide.shapes.title
    if holder is None:
        return ""
    return " ".join(str(holder.text or "").split())


def _shapes(shapes: Any) -> Iterator[str]:
    """Every shape's text, recursing into groups.

    A grouped shape holds its children rather than its own text, so a deck whose content
    was aligned with the group tool would otherwise extract as a title and nothing else.
    """
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _shapes(shape.shapes)
            continue
        if getattr(shape, "has_table", False):
            yield from _slide_table(shape.table)
            continue
        if not getattr(shape, "has_text_frame", False):
            continue
        for paragraph in shape.text_frame.paragraphs:
            text = "".join(run.text for run in paragraph.runs).strip()
            if text:
                yield text


def _slide_table(table: Any) -> Iterator[str]:
    rows = [[" ".join(str(cell.text or "").split()) for cell in row.cells] for row in table.rows]
    yield from _records(rows)


def _notes(slide: Any) -> str:
    if not slide.has_notes_slide:
        return ""
    frame = slide.notes_slide.notes_text_frame
    if frame is None:
        return ""
    return " ".join(str(frame.text or "").split())


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------


def extract_xlsx(data: bytes, *, name: str) -> Extracted:
    """One section per non-empty sheet, titled with the sheet name."""
    import openpyxl

    try:
        values = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        formulas = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    except Exception as exc:
        raise ExtractionError(
            f"This spreadsheet could not be read: {exc}. It may be corrupt, or it may be "
            "a pre-2007 .xls file saved with an .xlsx name.",
            reason="malformed_xlsx",
        ) from exc

    try:
        sections = tuple(_sheets(values, formulas))
    finally:
        values.close()
        formulas.close()
    return Extracted(sections=sections, page_count=len(sections))


def _sheets(values: Any, formulas: Any) -> Iterator[Section]:
    for title in values.sheetnames:
        rows = _rows(values[title], formulas[title])
        text = _sheet_text(rows)
        if text:
            yield Section(text=text, title=title)


def _rows(value_sheet: Any, formula_sheet: Any) -> list[list[str]]:
    """Both workbooks walked in lockstep, so a formula falls back to its own text.

    ``data_only=True`` returns the value Excel cached the last time it recalculated, which
    is what a reader of the file sees and therefore what should be indexed. It is absent
    when the file was written by a library rather than by a spreadsheet program, and then
    the formula itself is the only thing there is to say — ``=SUM(B2:B40)`` is at least
    true, where an empty cell would silently claim the column is blank.
    """
    rows: list[list[str]] = []
    pairs = zip(
        value_sheet.iter_rows(values_only=True),
        formula_sheet.iter_rows(values_only=True),
        strict=False,
    )
    for index, (cached, written) in enumerate(pairs):
        if index >= MAX_SHEET_ROWS:
            logger.info(
                "sheet truncated at the row cap",
                extra={"sheet": value_sheet.title, "cap": MAX_SHEET_ROWS},
            )
            rows.append([TRUNCATION_NOTICE.format(rows=MAX_SHEET_ROWS)])
            break
        rows.append([_cell(a, b) for a, b in zip(cached, written, strict=False)])
    return rows


def _cell(cached: Any, written: Any) -> str:
    value = cached if cached is not None else written
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and value.is_integer():
        # Excel stores every number as a float. "2" is what the sheet shows and "2.0" is
        # what a reader would not recognise as the same figure.
        return str(int(value))
    if isinstance(value, (datetime, date, time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return str(value).strip()


def _sheet_text(rows: Sequence[Sequence[str]]) -> str:
    """Header detection, then one record per row.

    The header is the first row with something in it, and it is only treated as a header
    when there is at least one row under it: a one-row sheet is data, and turning it into
    column names would index the labels and drop the content.
    """
    populated = [(index, row) for index, row in enumerate(rows) if any(cell for cell in row)]
    if not populated:
        return ""
    if len(populated) == 1:
        return "\n\n".join(_records([populated[0][1]]))

    header_index, header_row = populated[0]
    body = [row for index, row in populated if index > header_index]
    live = _live_columns(body)
    header = [
        (header_row[index] if index < len(header_row) else "") or f"column {index + 1}"
        for index in live
    ]

    records = []
    for row in body:
        record = render_record(header, [row[index] if index < len(row) else "" for index in live])
        if record:
            records.append(record)
    return "\n\n".join(records)


def _live_columns(rows: Sequence[Sequence[str]]) -> list[int]:
    """Columns with any value in them.

    A spreadsheet's used range routinely runs a hundred columns wider than its data, and a
    record carrying eighty empty column names is eighty tokens per row saying nothing.
    """
    width = max((len(row) for row in rows), default=0)
    return [index for index in range(width) if any(_at(row, index) for row in rows)]


def _at(row: Sequence[str], index: int) -> str:
    return row[index] if index < len(row) else ""


__all__ = [
    "MAX_SHEET_ROWS",
    "TRUNCATION_NOTICE",
    "extract_docx",
    "extract_pptx",
    "extract_xlsx",
]
