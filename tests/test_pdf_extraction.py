"""PDF extraction (task 11).

The assertions here are about the four things that decide whether a PDF is worth
embedding, and each of them is a failure that produces a document reporting ``indexed``
while answering badly:

* running headers and footers in every chunk, which flattens similarity scores;
* words broken across a line break, which embed as two tokens meaning nothing;
* two columns read across rather than down, which cuts every sentence in half;
* a scan indexed as a handful of near-empty chunks instead of being refused.

Fixtures are generated in :mod:`tests.office_fixtures`, so the property under test is
readable in source rather than sealed inside a committed binary.
"""

from __future__ import annotations

import pytest

from app.services.extraction import ExtractionError, SkippedDocument
from app.services.pdf import MIN_CHARS_PER_PAGE, extract_pdf
from tests.office_fixtures import (
    ANSWER,
    ANSWER_PAGE,
    BROKEN_PDF,
    HEADER,
    locked_pdf,
    manual_pdf,
    permissions_pdf,
    scanned_pdf,
    two_column_pdf,
)

PAGES = ANSWER_PAGE + 3


@pytest.fixture(scope="module")
def manual() -> object:
    return extract_pdf(manual_pdf(pages=PAGES), name="manual.pdf")


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


def test_every_page_becomes_a_section(manual: object) -> None:
    assert manual.page_count == PAGES  # type: ignore[attr-defined]
    assert len(manual.sections) == PAGES  # type: ignore[attr-defined]


def test_a_section_title_names_its_page(manual: object) -> None:
    """The acceptance criterion, in the form an end user checks: ``manual.pdf (p. 147)``
    renders from ``source_name`` plus this title, and the number has to be the page the
    sentence is actually on."""
    answering = [s for s in manual.sections if ANSWER in s.text]  # type: ignore[attr-defined]

    assert len(answering) == 1
    assert answering[0].title == f"Warranty > Coverage (p. {ANSWER_PAGE})"


def test_the_outline_labels_pages_after_the_bookmark_that_opened_them(manual: object) -> None:
    """A bookmark names where a chapter starts, so the pages between two of them belong to
    the earlier one. A lookup rather than a forward fill would leave every page but two
    unlabelled."""
    titles = [section.title for section in manual.sections]  # type: ignore[attr-defined]

    assert titles[0] == "Warranty (p. 1)"
    assert titles[ANSWER_PAGE - 2] == "Warranty (p. 146)"
    assert titles[ANSWER_PAGE] == "Warranty > Coverage (p. 148)"


def test_a_chunk_never_crosses_a_page_break(manual: object) -> None:
    """What makes the page in a citation true rather than nearly true.

    Run together and split on a token budget, a chunk drawn from pages 144 to 147 would be
    labelled with the page it happened to start on. An end user turns to that page, does
    not find the sentence, and stops believing the citations — which is worse than a label
    that is merely coarse.
    """
    assert manual.atomic_sections is True  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# boilerplate
# ---------------------------------------------------------------------------


def test_the_running_header_appears_in_no_chunk(manual: object) -> None:
    """Without this the confidentiality notice is in all 150 chunks, every chunk is partly
    about it, and similarity scores compress until retrieval stops discriminating."""
    assert not any(HEADER in section.text for section in manual.sections)  # type: ignore[attr-defined]


def test_the_running_footer_is_stripped_even_though_it_changes_every_page(
    manual: object,
) -> None:
    """ "Page 4 of 150" and "Page 5 of 150" are the same running footer. Matching on the
    literal text would strip nothing, and page numbers are the single most common piece of
    boilerplate there is."""
    assert not any("Page 4 of" in section.text for section in manual.sections)  # type: ignore[attr-defined]
    assert not any(section.text.startswith("Page ") for section in manual.sections)  # type: ignore[attr-defined]


def test_the_body_survives_the_stripping(manual: object) -> None:
    """The other half of the assertion above, and the one that would catch a rule so
    aggressive it took the content with it."""
    assert "maintenance manual" in manual.sections[0].text  # type: ignore[attr-defined]


def test_a_short_document_keeps_its_repeated_lines() -> None:
    """Repetition means nothing across two pages — every line of a cover page would
    qualify — so the rule does not run at all below three."""
    extracted = extract_pdf(manual_pdf(pages=2), name="short.pdf")

    assert all(HEADER in section.text for section in extracted.sections)


# ---------------------------------------------------------------------------
# reading the text
# ---------------------------------------------------------------------------


def test_a_word_split_across_a_line_break_is_rejoined(manual: object) -> None:
    """ "manufac-" and "turing" are two tokens that mean nothing, and "manufac-" is what a
    citation would show a reader."""
    text = manual.sections[0].text  # type: ignore[attr-defined]

    assert "manufacturing defects" in text
    assert "manufac" not in text.replace("manufacturing", "")


def test_two_columns_are_read_down_rather_than_across() -> None:
    """PDFium returns text in draw order, which interleaves the columns line by line. The
    sentence is the unit that gets embedded, so a sentence cut in half by the sentence
    beside it is the whole failure."""
    text = extract_pdf(two_column_pdf(), name="columns.pdf").sections[0].text

    left = text.index("left column sentence number 0")
    assert text.index("left column sentence number 13") < text.index("right column sentence")
    assert text.index("Installation and commissioning") < left


def test_control_characters_never_reach_the_index(manual: object) -> None:
    """PDFium emits them for glyphs with no Unicode mapping. A chunk carrying one renders
    as a box in the request drawer and embeds as nothing."""
    joined = "".join(section.text for section in manual.sections)  # type: ignore[attr-defined]

    assert not any(ord(character) < 32 and character != "\n" for character in joined)


# ---------------------------------------------------------------------------
# the cases that are not text
# ---------------------------------------------------------------------------


def test_a_scan_is_skipped_as_needing_ocr() -> None:
    """Not failed: nothing went wrong, and a retry reaches the same decision. Indexing it
    would produce a document that reports `indexed` with nothing in it, which is worse
    than a refusal because nothing looks wrong."""
    with pytest.raises(SkippedDocument) as raised:
        extract_pdf(scanned_pdf(), name="scan.pdf")

    assert raised.value.reason == "needs_ocr"
    assert "4 pages" in str(raised.value)
    assert "text layer" in str(raised.value)


def test_the_ocr_floor_is_an_average_rather_than_a_per_page_test() -> None:
    """A manual with a dozen full-page diagrams in it is still a text document. Testing
    each page would skip it, and skipping a real corpus is worse than indexing a few thin
    pages out of one."""
    extracted = extract_pdf(manual_pdf(pages=PAGES), name="manual.pdf")
    thin = [s for s in extracted.sections if len(s.text) < MIN_CHARS_PER_PAGE]

    assert extracted.sections  # it was not skipped
    assert thin == []  # and this fixture has none, so the average is what carried it


def test_a_password_protected_pdf_says_so_and_does_not_ask_for_the_password() -> None:
    with pytest.raises(ExtractionError) as raised:
        extract_pdf(locked_pdf(), name="locked.pdf")

    assert raised.value.reason == "password_protected"
    assert "does not store document passwords" in str(raised.value)


def test_a_pdf_with_only_an_owner_password_is_read() -> None:
    """The reason the empty-password attempt is made at all. "You may read this but not
    print it" is encrypted, opens with no password, and is most of the corporate documents
    anybody actually has."""
    extracted = extract_pdf(permissions_pdf(), name="permissions.pdf")

    assert ANSWER in extracted.text


def test_a_truncated_pdf_fails_with_a_sentence() -> None:
    with pytest.raises(ExtractionError) as raised:
        extract_pdf(BROKEN_PDF, name="broken.pdf")

    assert raised.value.reason == "malformed_pdf"
    assert "truncated or corrupt" in str(raised.value)


def test_the_reason_survives_being_pickled() -> None:
    """Isolated extraction returns its exception across a process boundary, and the default
    reconstruction calls ``cls(*args)`` — which drops a keyword-only field. Without
    ``__reduce__`` every ``needs_ocr`` decided in a subprocess would arrive as a generic
    failure, and only in the formats that are isolated, which are the ones that needed the
    reasons."""
    import pickle

    original = SkippedDocument("no text layer", reason="needs_ocr")

    restored = pickle.loads(pickle.dumps(original))

    assert isinstance(restored, SkippedDocument)
    assert restored.reason == "needs_ocr"
    assert str(restored) == "no text layer"
