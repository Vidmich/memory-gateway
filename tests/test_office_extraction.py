"""Word, PowerPoint and Excel extraction (task 11).

Each format has one or two places where the obvious implementation is quietly wrong, and
those are what most of these assert. The tracked-changes case is the one to read first: it
is invisible, it produces a document that reports ``indexed`` with a plausible chunk count,
and what it indexes is the draft somebody spent a review cycle correcting.
"""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from app.services.extraction import ExtractionError
from app.services.office import MAX_SHEET_ROWS, extract_docx, extract_pptx, extract_xlsx
from tests.office_fixtures import BROKEN_ZIP, PRODUCT, deck_pptx, policy_docx, prices_xlsx


def section(extracted: object, title: str) -> str:
    for entry in extracted.sections:  # type: ignore[attr-defined]
        if entry.title == title:
            return str(entry.text)
    raise AssertionError(f"no section titled {title!r}")


# ---------------------------------------------------------------------------
# Word
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def policy() -> object:
    return extract_docx(policy_docx(), name="policy.docx")


def test_a_section_is_titled_with_its_whole_heading_path(policy: object) -> None:
    """ "Access Control" tells a reader nothing on its own; the path says where in the
    document they landed."""
    assert section(policy, "Security > Access Control").startswith("Access Control")


def test_a_tracked_insertion_is_kept_and_a_deletion_is_dropped(policy: object) -> None:
    """The failure this exists for is silent. ``python-docx`` reads a paragraph by walking
    the runs directly beneath it, and an inserted run lives inside ``w:ins`` — so a policy
    document that has been through review indexes as its pre-review draft, with a
    plausible chunk count and nothing looking wrong."""
    warranty = section(policy, "Warranty")

    assert "Coverage now includes shipping." in warranty
    assert "excludes shipping" not in warranty


def test_a_footnote_is_appended_to_the_paragraph_that_references_it(policy: object) -> None:
    """Appended rather than inlined: a footnote spliced into a sentence breaks the
    sentence, and the sentence is what gets embedded."""
    warranty = section(policy, "Warranty")

    assert f"The {PRODUCT} carries a thirty-six month warranty." in warranty
    assert "(Measured at the Utrecht depot.)" in warranty


def test_word_scaffolding_is_not_mistaken_for_a_footnote(policy: object) -> None:
    """The rule drawn above the notes is a ``w:footnote`` too. Indexing it would put the
    word "sep" into a document about warranties."""
    assert "sep" not in section(policy, "Warranty").split()


def test_a_list_item_keeps_its_marker(policy: object) -> None:
    assert "- Badge readers log every entry." in section(policy, "Security > Access Control")


def test_a_table_is_rendered_as_records(policy: object) -> None:
    """The CSV convention from task 09, by the same function: a value without its column
    name is a string with no meaning attached."""
    assert "component: intake filter\ninterval: quarterly" in section(policy, "Warranty")


def test_a_table_stays_under_the_heading_it_belongs_to(policy: object) -> None:
    """``document.paragraphs`` and ``document.tables`` are separate lists, so reading them
    in turn moves every table to the end of the file and detaches it from its heading."""
    assert "intake filter" in section(policy, "Warranty")
    assert "intake filter" not in section(policy, "Security > Access Control")


def test_word_reports_no_page_count(policy: object) -> None:
    """Pagination is decided by the renderer from the fonts and the paper size. A number
    here would be invented, and `None` says so."""
    assert policy.page_count is None  # type: ignore[attr-defined]


def test_a_corrupt_word_file_fails_with_a_sentence() -> None:
    with pytest.raises(ExtractionError) as raised:
        extract_docx(BROKEN_ZIP, name="policy.docx")

    assert raised.value.reason == "malformed_docx"
    assert "pre-2007 .doc file" in str(raised.value)


# ---------------------------------------------------------------------------
# PowerPoint
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def deck() -> object:
    return extract_pptx(deck_pptx(), name="deck.pptx")


def test_a_slide_is_titled_by_its_number_and_its_title(deck: object) -> None:
    assert [entry.title for entry in deck.sections] == [  # type: ignore[attr-defined]
        "Slide 1: Rollout",
        "Slide 2: Pricing",
    ]


def test_speaker_notes_are_included_and_labelled(deck: object) -> None:
    """The slide says "Rollout" over two bullets; the sentence that says what was actually
    decided is in the notes. The label is what tells a model it is quoting the presenter
    rather than the screen."""
    first = section(deck, "Slide 1: Rollout")

    assert f"Notes: The {PRODUCT} rollout was delayed to the second quarter." in first


def test_a_slide_table_becomes_records(deck: object) -> None:
    assert "sku: QX-4471\nlist price: 1990" in section(deck, "Slide 2: Pricing")


def test_a_deck_claims_its_slides_are_atomic(deck: object) -> None:
    """A slide is a unit somebody authored. Gluing two into one chunk produces a chunk
    about two subjects, which is the failure chunking exists to prevent — so the extractor
    says so and the chunker keeps them apart whatever the connector's strategy is."""
    assert deck.atomic_sections is True  # type: ignore[attr-defined]
    assert deck.page_count == 2  # type: ignore[attr-defined]


def test_a_corrupt_presentation_fails_with_a_sentence() -> None:
    with pytest.raises(ExtractionError) as raised:
        extract_pptx(BROKEN_ZIP, name="deck.pptx")

    assert raised.value.reason == "malformed_pptx"
    assert "pre-2007 .ppt file" in str(raised.value)


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def prices() -> object:
    return extract_xlsx(prices_xlsx(), name="prices.xlsx")


def test_a_sheet_is_titled_with_its_name(prices: object) -> None:
    assert [entry.title for entry in prices.sections] == ["Prices", "Notes"]  # type: ignore[attr-defined]


def test_rows_are_records_under_the_header_row(prices: object) -> None:
    assert "sku: QX-4471\nname: Zynthorp QX-4471 variant 1\nlist price: 1991" in section(
        prices, "Prices"
    )


def test_a_column_with_nothing_in_it_is_dropped(prices: object) -> None:
    """A spreadsheet's used range routinely runs far wider than its data, and a record
    carrying empty column names is tokens per row saying nothing."""
    assert "discontinued" not in section(prices, "Prices")


def test_an_integer_valued_cell_is_not_rendered_as_a_float(prices: object) -> None:
    """Excel stores every number as a float. "1990" is what the sheet shows; "1990.0" is
    what a reader would not recognise as the same figure."""
    text = section(prices, "Prices")

    assert "1990" in text
    assert "1990.0" not in text


def test_a_formula_with_no_cached_value_records_its_formula(prices: object) -> None:
    """``data_only=True`` returns what Excel cached last time it recalculated, which is
    absent when a library wrote the file. ``=SUM(C2:C4)`` is at least true, where an empty
    cell would claim the column is blank."""
    assert "=SUM(C2:C4)" in section(prices, "Prices")


def test_an_empty_sheet_produces_no_section(prices: object) -> None:
    assert "Empty" not in [entry.title for entry in prices.sections]  # type: ignore[attr-defined]
    assert prices.page_count == 2  # type: ignore[attr-defined]


def test_a_cached_value_beats_the_formula_that_produced_it() -> None:
    """The other side of the fallback above: what a reader of the file sees is the value,
    so that is what should be indexed."""
    book = Workbook()
    sheet = book.active
    sheet.append(["region", "total"])
    sheet.append(["Utrecht", "=SUM(B4:B9)"])
    buffer = io.BytesIO()
    book.save(buffer)
    # openpyxl writes no cache, so the cached value is planted by loading and re-saving
    # through the value view — the same state Excel leaves behind.
    data = _with_cached_value(buffer.getvalue())

    assert "total: 4711" in extract_xlsx(data, name="totals.xlsx").text


def _with_cached_value(data: bytes) -> bytes:
    """Replace the formula's cached value, which is what a spreadsheet program writes."""
    import re
    import zipfile

    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for item in source.infolist():
            body = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                body = re.sub(rb"<f>SUM\(B4:B9\)</f>", rb"<f>SUM(B4:B9)</f><v>4711</v>", body)
            target.writestr(item, body)
    return out.getvalue()


def test_a_sheet_past_the_row_cap_is_truncated_and_says_so() -> None:
    """A spreadsheet is usually a database export. Indexing all of it produces near-
    identical chunks that crowd every prose document out of the index — and the notice is
    in the text rather than only in the logs, so a reader of the chunk sees it too."""
    book = Workbook()
    sheet = book.active
    sheet.append(["sku", "price"])
    for index in range(12):
        sheet.append([f"sku-{index}", index])
    buffer = io.BytesIO()
    book.save(buffer)

    import app.services.office as office

    original = office.MAX_SHEET_ROWS
    office.MAX_SHEET_ROWS = 5
    try:
        text = extract_xlsx(buffer.getvalue(), name="big.xlsx").text
    finally:
        office.MAX_SHEET_ROWS = original

    assert "sku: sku-0" in text
    assert "sku: sku-9" not in text
    assert "only the first 5 rows" in text


def test_the_row_cap_default_is_the_documented_one() -> None:
    """Asserted because the test above moves it, and a default that quietly changed would
    otherwise be invisible."""
    assert MAX_SHEET_ROWS == 50_000


def test_a_corrupt_workbook_fails_with_a_sentence() -> None:
    with pytest.raises(ExtractionError) as raised:
        extract_xlsx(BROKEN_ZIP, name="prices.xlsx")

    assert raised.value.reason == "malformed_xlsx"
    assert "pre-2007 .xls file" in str(raised.value)
