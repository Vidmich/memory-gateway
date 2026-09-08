"""The four formats through the whole pipeline, and whether what comes out is worth
embedding (task 11).

Everything above this file tests an extractor against bytes. This one runs the real
:class:`~app.services.ingestion.IngestionPipeline` over the fixture corpus — sniffing,
extraction, chunking, embedding, indexing — and then asks the index questions.

:data:`~tests.office_fixtures.QUESTIONS` is the labelled set the task's closing note asks
for. It is small and it is not a benchmark: what it catches is a dependency upgrade that
starts returning page furniture instead of prose, which otherwise surfaces months later as
"retrieval got worse" with no commit to point at.
"""

from __future__ import annotations

import pytest

from app.services.filetypes import DOCX, PDF, PPTX, XLSX
from app.services.jobs import HEAVY_QUEUE
from tests.auth_support import make_organization
from tests.connector_support import ConnectorFixture, build_connectors
from tests.office_fixtures import (
    ANSWER,
    ANSWER_PAGE,
    BROKEN_ZIP,
    HEADER,
    PRODUCT,
    QUESTIONS,
    corpus,
    scanned_pdf,
)


@pytest.fixture
def connectors() -> ConnectorFixture:
    """A wider embedder than the rest of the suite uses.

    The local embedder hashes words into buckets, and this corpus indexes a few hundred
    chunks. At the default 64 buckets they collide often enough that the ranking is about
    the hash rather than about the text, which would make the labelled questions below a
    test of arithmetic. A real embedding model is 1536 dimensions and this is closer to it.
    """
    return build_connectors(make_organization(), dimension=512)


@pytest.fixture
async def indexed(connectors: ConnectorFixture) -> ConnectorFixture:
    await connectors.ingest(*corpus())
    return connectors


# ---------------------------------------------------------------------------
# the four formats reach the index
# ---------------------------------------------------------------------------


async def test_every_format_indexes(indexed: ConnectorFixture) -> None:
    assert await indexed.statuses() == {
        "manual.pdf": "indexed",
        "policy.docx": "indexed",
        "deck.pptx": "indexed",
        "prices.xlsx": "indexed",
    }


async def test_each_document_reports_the_type_its_bytes_are(indexed: ConnectorFixture) -> None:
    """Sniffed, not taken from the extension — a ZIP is a ZIP until its name refines it,
    and the refinement is what routes it to the right parser."""
    types = {document.source_name: document.mime_type for document in await indexed.documents()}

    assert types == {
        "manual.pdf": PDF,
        "policy.docx": DOCX,
        "deck.pptx": PPTX,
        "prices.xlsx": XLSX,
    }


async def test_page_slide_and_sheet_counts_reach_the_row(indexed: ConnectorFixture) -> None:
    """Each format's own unit, with the noun left to the UI. Word is `None` rather than a
    number, because its pagination is a rendering decision and any figure here would be
    invented."""
    counts = {document.source_name: document.page_count for document in await indexed.documents()}

    assert counts["manual.pdf"] == ANSWER_PAGE + 3
    assert counts["deck.pptx"] == 2
    assert counts["prices.xlsx"] == 2  # the empty third sheet is not a sheet worth counting
    assert counts["policy.docx"] is None


async def test_chunk_counts_are_sensible(indexed: ConnectorFixture) -> None:
    """A number that is *plausible* is the thing to assert here. Zero means extraction
    silently produced nothing; one per document means the sections were glued together;
    thousands means a spreadsheet was indexed cell by cell."""
    chunks = {document.source_name: document.chunk_count for document in await indexed.documents()}

    # One per page, because a chunk that crosses a page break cannot cite a page.
    assert chunks["manual.pdf"] == ANSWER_PAGE + 3
    # One per slide, because a slide is a unit somebody authored.
    assert chunks["deck.pptx"] == 2
    assert 1 <= chunks["prices.xlsx"] <= 4
    assert 1 <= chunks["policy.docx"] <= 4


# ---------------------------------------------------------------------------
# what the index actually holds
# ---------------------------------------------------------------------------


async def test_a_citation_from_the_pdf_names_the_page_the_sentence_is_on(
    indexed: ConnectorFixture,
) -> None:
    """The acceptance criterion, checked against the source: the fixture writes that
    sentence on that page, and this is the label a reader would be told to turn to."""
    document = await indexed.document("manual.pdf")
    found = await indexed.service.document_chunks(indexed.actor, document.id, limit=500)
    answering = [chunk for chunk in found.chunks if ANSWER in chunk.text]

    assert len(answering) == 1
    assert answering[0].payload["page_or_section"] == f"Warranty > Coverage (p. {ANSWER_PAGE})"


async def test_no_chunk_carries_the_running_header(indexed: ConnectorFixture) -> None:
    """Retrieval quality, asserted where it is decided. With the header in all 150 chunks,
    every chunk is partly about ACME's confidentiality notice and similarity scores
    compress until nothing discriminates."""
    document = await indexed.document("manual.pdf")
    found = await indexed.service.document_chunks(indexed.actor, document.id, limit=500)

    assert found.chunks
    assert not any(HEADER in chunk.text for chunk in found.chunks)


async def test_the_inspector_agrees_with_the_row(indexed: ConnectorFixture) -> None:
    """The two disagreeing is the finding the inspector exists to surface."""
    document = await indexed.document("deck.pptx")
    found = await indexed.service.document_chunks(indexed.actor, document.id)

    assert found.chunk_count == document.chunk_count == len(found.chunks)
    assert [chunk.payload["page_or_section"] for chunk in found.chunks] == [
        "Slide 1: Rollout",
        "Slide 2: Pricing",
    ]


async def test_speaker_notes_are_in_the_index_not_only_in_the_extractor(
    indexed: ConnectorFixture,
) -> None:
    """The end-to-end half of the notes decision: the sentence that says what was decided
    has to be searchable, not merely extracted."""
    hits = await indexed.service.search(indexed.actor, indexed.connector.id, "rollout delayed")

    assert any("delayed to the second quarter" in hit.text for hit in hits)


# ---------------------------------------------------------------------------
# the labelled question set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("question", "expected"), QUESTIONS)
async def test_the_labelled_questions_reach_the_right_document(
    indexed: ConnectorFixture, question: str, expected: str
) -> None:
    """Which *file* answers, and whether it is still reachable at all.

    Membership in the top three rather than first place, and the reason is the embedder
    rather than the corpus: the local one hashes words into buckets, so a 150-page manual
    that shares only "of" and "the" with a question can still out-score the spreadsheet
    that literally contains the answer. Ranking under a toy embedder is a fact about the
    hash. Presence is not — a format whose extraction quietly starts returning page
    furniture instead of prose drops out of these results entirely, which is the
    regression this set exists to catch and the one nothing else in the suite would see.
    """
    hits = await indexed.service.search(indexed.actor, indexed.connector.id, question, limit=3)

    assert hits, question
    assert expected in {hit.payload["source_name"] for hit in hits}, question


async def test_the_product_code_is_only_answerable_from_the_corpus(
    indexed: ConnectorFixture,
) -> None:
    """The same trick task 10 used, applied to extraction: the code is in no model's
    training data, so a hit for it can only have come from these documents."""
    hits = await indexed.service.search(indexed.actor, indexed.connector.id, PRODUCT, limit=5)

    assert {hit.payload["source_name"] for hit in hits} >= {"manual.pdf", "prices.xlsx"}


# ---------------------------------------------------------------------------
# the documents that do not index
# ---------------------------------------------------------------------------


async def test_a_scan_lands_as_skipped_needing_ocr(connectors: ConnectorFixture) -> None:
    """End to end, because the reason has to survive the pipeline as well as the
    extractor: `skipped` with a code the UI can turn into an explanation, rather than
    `indexed` with four empty chunks."""
    await connectors.ingest(("scan.pdf", scanned_pdf()))

    document = await connectors.document("scan.pdf")
    assert document.status == "skipped"
    assert document.reason == "needs_ocr"
    assert document.chunk_count == 0


@pytest.mark.parametrize("name", ["policy.docx", "deck.pptx", "prices.xlsx"])
async def test_a_malformed_office_file_fails_without_stopping_the_others(
    connectors: ConnectorFixture, name: str
) -> None:
    """A corrupt file in a folder of good ones fails alone. The Markdown beside it must
    still index, which is the thing a worker crash would take away."""
    await connectors.ingest((name, BROKEN_ZIP), ("notes.md", b"# Notes\n\nAll fine here."))

    broken = await connectors.document(name)
    assert broken.status == "failed"
    assert broken.reason is not None and broken.reason.startswith("malformed_")
    assert (await connectors.document("notes.md")).status == "indexed"


# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------


async def test_heavy_formats_are_enqueued_on_their_own_queue(
    connectors: ConnectorFixture,
) -> None:
    """Otherwise a folder of notes dropped alongside a 300-page manual sits at ``pending``
    behind it, for no reason a customer can see. Decided from the *name*, because this is
    a scheduling call made before any bytes have been read — and being wrong about one
    costs ordering and nothing else."""
    await connectors.upload(("manual.pdf", b"%PDF-1.7\n"), ("notes.md", b"# Notes"))

    queues = {job.payload["document_id"]: job.queue for job in connectors.queue.submitted}
    documents = {d.source_name: str(d.id) for d in await connectors.documents()}

    assert queues[documents["manual.pdf"]] == HEAVY_QUEUE
    assert queues[documents["notes.md"]] is None
