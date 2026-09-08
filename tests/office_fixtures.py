"""The task 11 fixture corpus, written rather than committed.

Every file here is *generated*, and that is the point. A PDF with a running header at a
known height on a known page, a Word file whose third paragraph is a tracked insertion, a
spreadsheet whose formula has no cached value — those are the cases extraction has to get
right, and a checked-in binary asserts them in a form nobody can read or change. Written
fixtures make the property being tested visible in the same file as the test.

They are also a corpus rather than a set of unrelated samples. One imaginary product runs
through all four formats — the **Zynthorp QX-4471**, a string that exists in no model's
training data and in no other test — so a question answered from it can only have been
answered from these documents. :data:`QUESTIONS` is the labelled set the note in the task
asks for: the cheapest way to notice that a dependency upgrade quietly degraded
extraction, which otherwise shows up as "retrieval got worse" months later.

Building is not free — a 200-page PDF is about a second — so anything expensive is cached
per process by :func:`functools.cache`. The builders take no arguments where they can help
it, for exactly that reason.
"""

from __future__ import annotations

import io
from functools import cache

from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches
from reportlab.lib import pdfencrypt
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

WIDTH, HEIGHT = letter

#: In no model's training data, and in no other fixture. An answer containing it came from
#: these documents or from nowhere.
PRODUCT = "Zynthorp QX-4471"

#: The running header and footer every page of the manual carries. The footer changes on
#: every page, which is the case naive de-boilerplating misses.
HEADER = "ACME Corporation - Confidential"
FOOTER = "Page {page} of {total}"

#: The page the demo's question is answered on. Deliberately deep into the document, so a
#: citation naming it is a citation and not a coincidence.
ANSWER_PAGE = 147

#: What that page says. One sentence, with the product code in it.
ANSWER = f"The {PRODUCT} ships from the Utrecht depot and is covered for thirty-six months."


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


@cache
def manual_pdf(pages: int = 200) -> bytes:
    """The product manual: running header and footer, an outline, hyphenation, and one
    page that answers a question nothing else in the corpus can."""
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=letter)
    for number in range(1, pages + 1):
        page.bookmarkPage(f"p{number}")
        if number == 1:
            page.addOutlineEntry("Warranty", "p1", level=0)
        if number == ANSWER_PAGE:
            page.addOutlineEntry("Coverage", f"p{ANSWER_PAGE}", level=1)

        page.setFont("Helvetica", 9)
        page.drawString(72, HEIGHT - 40, HEADER)
        page.drawString(72, 30, FOOTER.format(page=number, total=pages))

        page.setFont("Helvetica", 11)
        top = HEIGHT - 100
        for line in _body(number):
            page.drawString(72, top, line)
            top -= 16
        page.showPage()
    page.save()
    return buffer.getvalue()


def _body(number: int) -> list[str]:
    lines = [
        f"Section {number} of the maintenance manual.",
        # Split across the line break on purpose: this is the hyphenation case, and the
        # word it makes is what a citation would otherwise show a reader.
        "The warranty covers manufac-",
        "turing defects for two years from the date of purchase.",
    ]
    if number == ANSWER_PAGE:
        lines.append(ANSWER)
    return lines


@cache
def two_column_pdf() -> bytes:
    """A full-width heading over two columns, each sentence split across the pair.

    The assertion this exists for is that a sentence survives: read in raw draw order, the
    columns interleave and every sentence is cut in half by the one beside it.
    """
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=letter)
    page.setFont("Helvetica", 12)
    page.drawString(72, HEIGHT - 60, "Installation and commissioning of the depot units")
    page.setFont("Helvetica", 10)
    top = HEIGHT - 100
    for index in range(14):
        page.drawString(72, top, f"left column sentence number {index} continues onward")
        page.drawString(340, top, f"right column sentence number {index} continues onward")
        top -= 16
    page.showPage()
    page.save()
    return buffer.getvalue()


@cache
def scanned_pdf(pages: int = 4) -> bytes:
    """Pages that are pictures. No text layer at all, which is what ``needs_ocr`` means."""
    from PIL import Image

    picture = io.BytesIO()
    Image.new("RGB", (400, 400), "white").save(picture, format="PNG")

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=letter)
    for _ in range(pages):
        picture.seek(0)
        page.drawImage(ImageReader(picture), 72, 300, width=400, height=400)
        page.showPage()
    page.save()
    return buffer.getvalue()


@cache
def locked_pdf() -> bytes:
    """Encrypted with a *user* password, so an empty-password open cannot succeed."""
    return _one_page(pdfencrypt.StandardEncryption("secret"))


@cache
def permissions_pdf() -> bytes:
    """Encrypted with an *owner* password only — "you may read this but not print it".

    The case that makes the empty-password attempt worth making: it is encrypted, it opens
    with no password at all, and refusing it would skip a large share of the corporate
    documents anybody actually has.
    """
    return _one_page(pdfencrypt.StandardEncryption("", ownerPassword="owner", canPrint=0))


def _one_page(encryption: object) -> bytes:
    """One encrypted page carrying the answer plus enough prose around it.

    "Enough" is deliberate: a page with one sentence on it is below the characters-per-page
    floor and would be skipped as a scan, which would make these two tests pass or fail for
    a reason that has nothing to do with encryption.
    """
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=letter, encrypt=encryption)
    page.setFont("Helvetica", 11)
    top = HEIGHT - 100
    for line in [*_body(1), ANSWER]:
        page.drawString(72, top, line)
        top -= 16
    page.showPage()
    page.save()
    return buffer.getvalue()


#: Claims to be a PDF in its first five bytes and is not one.
BROKEN_PDF = b"%PDF-1.7\n" + bytes(9000)

#: A ZIP that is not an Office document. Sniffs as whichever Office type its name says,
#: which is what makes it reach a parser at all.
BROKEN_ZIP = b"PK\x03\x04" + bytes(9000)


# ---------------------------------------------------------------------------
# Word
# ---------------------------------------------------------------------------

_FOOTNOTES_XML = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    b'<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    b'<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:t>sep</w:t></w:r></w:p></w:footnote>'
    b'<w:footnote w:id="1"><w:p><w:r><w:t>Measured at the Utrecht depot.</w:t>'
    b"</w:r></w:p></w:footnote>"
    b"</w:footnotes>"
)

_FOOTNOTES_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"


@cache
def policy_docx() -> bytes:
    """Nested headings, a list, a table, a tracked change, and a footnote.

    The tracked change is the one worth reading. ``inserted`` is inside ``w:ins`` and
    ``removed`` inside ``w:del``; a reader that walks a paragraph's direct children sees
    neither, and the document indexes as its pre-review draft with nothing looking wrong.
    """
    import docx
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    document = docx.Document()
    document.add_heading("Security", level=1)
    document.add_heading("Access Control", level=2)
    document.add_paragraph("Depot doors are locked outside operating hours.")
    document.add_paragraph("Badge readers log every entry.", style="List Bullet")

    document.add_heading("Warranty", level=1)
    body = document.add_paragraph(f"The {PRODUCT} carries a thirty-six month warranty.")
    body._p.append(parse_xml(f'<w:r {nsdecls("w")}><w:footnoteReference w:id="1"/></w:r>'))

    revised = document.add_paragraph()
    revised._p.append(
        parse_xml(
            f'<w:ins {nsdecls("w")} w:id="900" w:author="R" w:date="2026-01-01T00:00:00Z">'
            "<w:r><w:t>Coverage now includes shipping.</w:t></w:r></w:ins>"
        )
    )
    revised._p.append(
        parse_xml(
            f'<w:del {nsdecls("w")} w:id="901" w:author="R" w:date="2026-01-01T00:00:00Z">'
            "<w:r><w:delText>Coverage excludes shipping.</w:delText></w:r></w:del>"
        )
    )

    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "component"
    table.cell(0, 1).text = "interval"
    table.cell(1, 0).text = "intake filter"
    table.cell(1, 1).text = "quarterly"

    package = document.part.package
    footnotes = Part(PackURI("/word/footnotes.xml"), _FOOTNOTES_TYPE, _FOOTNOTES_XML, package)
    document.part.relate_to(footnotes, RT.FOOTNOTES)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# PowerPoint
# ---------------------------------------------------------------------------


@cache
def deck_pptx() -> bytes:
    """Two slides: one with speaker notes, one with a table."""
    deck = Presentation()

    first = deck.slides.add_slide(deck.slide_layouts[1])
    first.shapes.title.text = "Rollout"
    first.placeholders[1].text = "Two depots in scope\nHardware arrives in March"
    first.notes_slide.notes_text_frame.text = (
        f"The {PRODUCT} rollout was delayed to the second quarter."
    )

    second = deck.slides.add_slide(deck.slide_layouts[5])
    second.shapes.title.text = "Pricing"
    table = second.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(5), Inches(1)).table
    table.cell(0, 0).text = "sku"
    table.cell(0, 1).text = "list price"
    table.cell(1, 0).text = "QX-4471"
    table.cell(1, 1).text = "1990"

    buffer = io.BytesIO()
    deck.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------


@cache
def prices_xlsx(rows: int = 3) -> bytes:
    """A header row, a formula with no cached value, an empty column, and an empty sheet."""
    book = Workbook()
    sheet = book.active
    sheet.title = "Prices"
    sheet.append(["sku", "name", "list price", "discontinued"])
    for index in range(rows):
        sheet.append([f"QX-447{index}", f"{PRODUCT} variant {index}", 1990 + index, None])
    # Written by a library, so there is no cached value for it and the formula text is the
    # only thing there is to index.
    sheet.cell(row=rows + 3, column=3, value="=SUM(C2:C4)")

    notes = book.create_sheet("Notes")
    notes.append([f"The {PRODUCT} is assembled in Utrecht."])
    book.create_sheet("Empty")

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# the labelled question set
# ---------------------------------------------------------------------------

#: Question, and the file that must still be reachable by it. Small on purpose: a
#: regression net for extraction quality, not a benchmark. What it catches is a dependency
#: upgrade that starts returning page furniture instead of prose — the kind of thing that
#: surfaces months later as "retrieval got worse" with no commit to point at. See
#: `tests/test_extraction_quality.py` for why the assertion is presence rather than rank.
QUESTIONS: tuple[tuple[str, str], ...] = (
    ("Which depot does the Zynthorp QX-4471 ship from?", "manual.pdf"),
    ("How long are manufacturing defects covered for?", "manual.pdf"),
    ("Are depot doors locked outside operating hours?", "policy.docx"),
    ("How often is the intake filter serviced?", "policy.docx"),
    ("Why was the Zynthorp QX-4471 rollout delayed?", "deck.pptx"),
    ("What is the list price of the QX-4471?", "prices.xlsx"),
)


#: The corpus, as the ingestion pipeline takes it.
def corpus() -> tuple[tuple[str, bytes], ...]:
    return (
        ("manual.pdf", manual_pdf(pages=ANSWER_PAGE + 3)),
        ("policy.docx", policy_docx()),
        ("deck.pptx", deck_pptx()),
        ("prices.xlsx", prices_xlsx()),
    )
