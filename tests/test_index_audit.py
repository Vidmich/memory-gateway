"""The audit arithmetic (task 103), over hand-built payload fixtures.

Every finding gets a planted case, and the counts are checked against a hand count: the
work item's acceptance criterion is that the report over a fixture matches
:func:`~app.services.chunking_preview.distribution` over the same points and that each
finding's count matches what a person would count.
"""

from __future__ import annotations

import math
import random
import uuid

from app.schemas.connector_config import ChunkingConfig
from app.services.chunking import Chunk
from app.services.chunking_preview import distribution
from app.services.index_audit import (
    AMBER,
    GREEN,
    RED,
    ChunkingReport,
    DocumentRecord,
    DriftSample,
    EmbeddingReport,
    Finding,
    FormatReport,
    Neighbour,
    PointRecord,
    audit_chunking,
    audit_embeddings,
    drift_cosines,
    histogram,
    report_json,
)

SENTENCE = "The travel policy covers flights, hotels and meals on the road. "


def doc(
    name: str = "handbook.md", *, mime: str = "text/markdown", size: int = 4096
) -> DocumentRecord:
    return DocumentRecord(id=str(uuid.uuid4()), source_name=name, mime_type=mime, size_bytes=size)


def point(
    document: DocumentRecord,
    index: int,
    *,
    tokens: int = 200,
    text: str | None = None,
    fingerprint: str = "fp1",
    vector: tuple[float, ...] = (),
    kind: str = "source",
) -> PointRecord:
    body = text if text is not None else (SENTENCE * max(1, tokens // 12)).strip()
    return PointRecord(
        id=f"{document.id}:{index}",
        document_id=document.id,
        chunk_index=index,
        token_count=tokens,
        text=body,
        kind=kind,
        fingerprint=fingerprint,
        vector=vector,
    )


def config(**values: object) -> ChunkingConfig:
    return ChunkingConfig.model_validate({"chunk_size": 400, "overlap": 0, **values})


# ---------------------------------------------------------------------------
# distribution parity
# ---------------------------------------------------------------------------


def test_the_whole_index_distribution_matches_compares_over_the_same_chunks() -> None:
    """The acceptance criterion: the same numbers Compare computes, over the same points,
    including the mid-sentence rule that skips a document's last chunk."""
    document = doc()
    texts = [
        "A complete sentence here.",
        "one that trails off without",
        "Another complete one!",
        "and the last chunk of the document",
    ]
    sizes = [50, 380, 120, 40]
    points = [
        point(document, index, tokens=size, text=text)
        for index, (text, size) in enumerate(zip(texts, sizes, strict=True))
    ]
    chunks = [
        Chunk(text=text, index=index, section=None, token_count=size)
        for index, (text, size) in enumerate(zip(texts, sizes, strict=True))
    ]

    report = audit_chunking(points, [document], config())

    assert report.distribution == distribution(chunks, config())
    assert report.distribution.mid_sentence == 1
    assert report.distribution.at_ceiling == 1


def test_the_histogram_is_bucketed_to_the_ceiling_and_the_last_bucket_is_open() -> None:
    document = doc()
    points = [point(document, i, tokens=t) for i, t in enumerate((10, 60, 60, 399, 400, 5000))]

    built = histogram(points, 400)

    assert built.bucket_tokens == 50
    assert sum(bucket.count for bucket in built.buckets) == 6
    assert built.buckets[0].count == 1
    assert built.buckets[1].count == 2
    assert built.buckets[-1].count == 1
    assert built.buckets[-1].lower == 550


# ---------------------------------------------------------------------------
# findings, one planted case each
# ---------------------------------------------------------------------------


def finding(report: ChunkingReport | EmbeddingReport | FormatReport, code: str) -> Finding:
    found = [f for f in report.findings if f.code == code]
    assert found, [f.code for f in report.findings]
    return found[0]


def test_short_chunks_are_counted_and_the_documents_behind_them_named() -> None:
    changelog = doc("CHANGELOG.md")
    handbook = doc("handbook.md")
    points = [point(changelog, i, tokens=12, text="- fixed a bug") for i in range(5)]
    points += [point(handbook, i, tokens=300) for i in range(5)]

    report = audit_chunking(points, [changelog, handbook], config())
    short = finding(report, "short_chunks")

    assert short.count == 5
    assert short.severity == RED  # half the index
    assert short.documents[0].source_name == "CHANGELOG.md"
    assert short.documents[0].count == 5
    assert short.action == "compare"


def test_single_chunk_documents_are_a_finding_with_the_documents_listed() -> None:
    one = doc("one.md")
    two = doc("two.md")
    points = [point(one, 0, tokens=300), point(two, 0, tokens=300), point(two, 1, tokens=300)]

    report = audit_chunking(points, [one, two], config())
    single = finding(report, "single_chunk_documents")

    assert single.count == 1
    assert [d.source_name for d in single.documents] == ["one.md"]


def test_chunks_at_the_ceiling_are_amber_under_semantic_and_green_under_fixed() -> None:
    document = doc()
    points = [point(document, i, tokens=390) for i in range(4)]

    semantic = audit_chunking(points, [document], config(strategy="semantic"))
    fixed = audit_chunking(points, [document], config(strategy="fixed"))

    assert finding(semantic, "at_ceiling").severity == AMBER
    assert finding(fixed, "at_ceiling").severity == GREEN
    assert finding(fixed, "at_ceiling").count == 4


def test_a_chunk_far_over_the_ceiling_is_red_on_its_own() -> None:
    document = doc("minified.js", mime="text/javascript")
    points = [point(document, 0, tokens=9000), point(document, 1, tokens=100)]

    report = audit_chunking(points, [document], config())
    over = finding(report, "over_ceiling")

    assert over.severity == RED
    assert over.count == 1
    assert report.severity == RED


def test_mid_sentence_starts_count_only_on_prose_and_never_the_first_chunk() -> None:
    prose = doc("guide.md")
    code = doc("main.py", mime="text/x-python")
    points = [
        point(prose, 0, text="lowercase start on chunk zero is not counted."),
        point(prose, 1, text="and this one begins mid-sentence."),
        point(prose, 2, text="This one does not."),
        point(code, 0, text="def alpha():"),
        point(code, 1, text="return value"),
    ]

    report = audit_chunking(points, [prose, code], config())
    mid = finding(report, "mid_sentence_starts")

    assert mid.count == 1
    assert [d.source_name for d in mid.documents] == ["guide.md"]
    assert all(
        f.code != "mid_sentence_starts"
        for r in report.formats
        if r.kind == "code"
        for f in r.findings
    )


def test_exact_duplicates_across_documents_are_pairs_and_within_a_document_are_not() -> None:
    licence = "Licensed under the Apache License, Version 2.0; you may not use this file."
    a, b, c = (
        doc("a.py", mime="text/x-python"),
        doc("b.py", mime="text/x-python"),
        doc("c.py", mime="text/x-python"),
    )
    points = [
        point(a, 0, text=licence, tokens=20),
        point(b, 0, text=licence.upper(), tokens=20),  # case-folded: still a twin
        point(c, 0, text=licence, tokens=20),
        point(a, 1, text="def unique_a(): pass", tokens=20),
        point(a, 2, text="def unique_a(): pass", tokens=20),  # same doc: not this finding
    ]

    report = audit_chunking(points, [a, b, c], config())
    duplicates = finding(report, "duplicate_chunks")

    assert duplicates.count == 3  # three documents, three pairs
    assert duplicates.document_count == 3


def test_chunk_count_outliers_are_measured_against_the_formats_median() -> None:
    documents = [doc(f"d{i}.md", size=4096) for i in range(6)]
    points = []
    for document in documents[:5]:
        points += [point(document, i) for i in range(4)]
    # The sixth is the same size and forty chunks: ten times denser.
    points += [point(documents[5], i) for i in range(40)]

    report = audit_chunking(points, documents, config())
    outliers = finding(report, "chunk_count_outliers")

    assert outliers.count == 1
    assert outliers.documents[0].source_name == "d5.md"


def test_mixed_fingerprints_name_the_minority_and_point_at_the_reindex() -> None:
    old, new = doc("old.md"), doc("new.md")
    points = [point(old, i, fingerprint="aaa") for i in range(2)]
    points += [point(new, i, fingerprint="bbb") for i in range(5)]

    report = audit_chunking(points, [old, new], config())
    mixed = finding(report, "mixed_fingerprints")

    assert mixed.severity == AMBER
    assert mixed.count == 2
    assert mixed.action == "reindex"
    assert report.fingerprints == {"aaa": 2, "bbb": 5}


def test_the_report_is_per_format_as_well_as_overall() -> None:
    markdown = doc("guide.md")
    lockfile = doc("package-lock.json", mime="application/json")
    points = [point(markdown, i, tokens=300) for i in range(3)]
    points += [point(lockfile, i, tokens=15, text='"version": "1.0.0"') for i in range(30)]

    report = audit_chunking(points, [markdown, lockfile], config())

    assert [entry.kind for entry in report.formats] == ["json", "markdown"]
    json_report = next(entry for entry in report.formats if entry.kind == "json")
    assert json_report.distribution.median_tokens == 15
    assert finding(json_report, "short_chunks").count == 30
    markdown_report = next(entry for entry in report.formats if entry.kind == "markdown")
    assert all(f.code != "short_chunks" for f in markdown_report.findings)
    # The overall finding is the merged one.
    assert finding(report, "short_chunks").count == 30


def test_summary_points_are_counted_apart_and_never_judged() -> None:
    document = doc()
    points = [point(document, i, tokens=300) for i in range(3)]
    points.append(point(document, -1, tokens=30, text="A summary.", kind="summary"))

    report = audit_chunking(points, [document], config())

    assert report.points == 3
    assert report.summary_points == 1
    assert all(f.code != "short_chunks" for f in report.findings)


def test_a_healthy_index_has_no_findings_and_is_green() -> None:
    documents = [doc(f"d{i}.md") for i in range(3)]
    points = [
        point(d, i, tokens=280, text=f"Document {n} says something tidy in chunk {i}.")
        for n, d in enumerate(documents)
        for i in range(4)
    ]

    report = audit_chunking(points, documents, config())

    assert report.findings == ()
    assert report.severity == GREEN
    assert report_json(report)["severity"] == GREEN


# ---------------------------------------------------------------------------
# embeddings
# ---------------------------------------------------------------------------


def unit(seed: int, dimension: int = 8) -> tuple[float, ...]:
    generator = random.Random(seed)
    raw = [generator.gauss(0, 1) for _ in range(dimension)]
    norm = math.sqrt(sum(v * v for v in raw))
    return tuple(v / norm for v in raw)


def embedded(documents: int = 5, chunks: int = 4) -> tuple[list[DocumentRecord], list[PointRecord]]:
    rows = [doc(f"d{i}.md") for i in range(documents)]
    points = [
        point(row, index, text=f"chunk {i}-{index} of document {i}", vector=unit(i * 100 + index))
        for i, row in enumerate(rows)
        for index in range(chunks)
    ]
    return rows, points


def test_a_healthy_collection_passes_every_check() -> None:
    rows, points = embedded()
    neighbours = [
        Neighbour(p.id, p.document_id, f"{p.document_id}:0", p.document_id, 0.4) for p in points
    ]
    drift = [DriftSample(p.id, p.document_id, 0.995) for p in points]

    report = audit_embeddings(
        points,
        total_points=len(points),
        expected_dimension=8,
        expected_model="hash-bow",
        documents=[
            DocumentRecord(
                r.id, r.source_name, r.mime_type, r.size_bytes, embedding_model="hash-bow"
            )
            for r in rows
        ],
        neighbours=neighbours,
        drift=drift,
    )

    assert report.findings == ()
    assert report.severity == GREEN
    assert report.agreement is not None and report.agreement.rate == 1.0
    assert report.drift is not None and report.drift.shape == "healthy"


def test_a_tenth_of_identical_vectors_is_red() -> None:
    rows, points = embedded(documents=10, chunks=10)
    padding = unit(999)
    # Ten of a hundred, spread over documents, same vector for different text.
    planted = [
        PointRecord(p.id, p.document_id, p.chunk_index, p.token_count, p.text, vector=padding)
        if index % 10 == 0
        else p
        for index, p in enumerate(points)
    ]

    report = audit_embeddings(
        planted, total_points=100, expected_dimension=8, expected_model="hash-bow", documents=rows
    )
    identical = finding(report, "identical_vectors")

    assert identical.count == 10
    assert identical.severity == RED
    assert report.identical_vectors == 10


def test_a_stored_width_that_differs_from_the_setting_is_red() -> None:
    rows, points = embedded(documents=2)

    report = audit_embeddings(
        points, total_points=8, expected_dimension=1536, expected_model="hash-bow", documents=rows
    )
    mismatch = finding(report, "dimension_mismatch")

    assert mismatch.severity == RED
    assert mismatch.count == 8
    assert mismatch.action == "platform"
    assert report.dimensions == {8: 8}


def test_a_sample_that_re_embeds_to_cosine_point_six_is_drift() -> None:
    rows, points = embedded(documents=2)
    stored = points[:4]
    fresh = [
        tuple(v * 0.6 + 0.8 * o for v, o in zip(p.vector, unit(555), strict=True)) for p in stored
    ]
    samples = drift_cosines(stored, fresh)
    assert all(s.cosine < 0.95 for s in samples)

    report = audit_embeddings(
        points,
        total_points=8,
        expected_dimension=8,
        expected_model="text-embedding-3-small",
        documents=rows,
        drift=samples,
    )
    drift = finding(report, "drift")

    assert drift.severity == RED
    assert report.drift is not None
    assert report.drift.shape in ("offset", "low")
    assert (
        "text-embedding-3-small" in drift.detail or report.drift.model == "text-embedding-3-small"
    )


def test_half_a_sample_disagreeing_is_named_as_two_models() -> None:
    rows, points = embedded(documents=2, chunks=5)
    samples = [
        DriftSample(p.id, p.document_id, 0.99 if index < 5 else 0.7)
        for index, p in enumerate(points)
    ]

    report = audit_embeddings(
        points,
        total_points=10,
        expected_dimension=8,
        expected_model="m",
        documents=rows,
        drift=samples,
    )

    assert report.drift is not None and report.drift.shape == "bimodal"
    assert "two embedding models" in finding(report, "drift").detail


def test_low_agreement_on_alike_documents_says_that_can_be_fine() -> None:
    """Every document is about the same thing: the nearest neighbour is honestly
    elsewhere, the report says so, and it is amber rather than red."""
    rows, points = embedded(documents=4, chunks=3)
    other = {
        rows[0].id: rows[1].id,
        rows[1].id: rows[2].id,
        rows[2].id: rows[3].id,
        rows[3].id: rows[0].id,
    }
    neighbours = [Neighbour(p.id, p.document_id, "x", other[p.document_id], 0.97) for p in points]

    report = audit_embeddings(
        points,
        total_points=12,
        expected_dimension=8,
        expected_model="m",
        documents=rows,
        neighbours=neighbours,
    )
    low = finding(report, "low_agreement")

    assert report.agreement is not None and report.agreement.rate == 0.0
    assert low.severity == AMBER
    assert "That can be fine" in low.detail


def test_low_agreement_on_unalike_documents_is_red() -> None:
    rows, points = embedded(documents=4, chunks=3)
    neighbours = [
        Neighbour(p.id, p.document_id, "x", rows[(i + 1) % 4].id, 0.2) for i, p in enumerate(points)
    ]

    report = audit_embeddings(
        points,
        total_points=12,
        expected_dimension=8,
        expected_model="m",
        documents=rows,
        neighbours=neighbours,
    )

    assert finding(report, "low_agreement").severity == RED


def test_zero_vectors_and_a_model_mismatch_are_found() -> None:
    rows, points = embedded(documents=2, chunks=2)
    zeroed = [
        PointRecord(p.id, p.document_id, p.chunk_index, p.token_count, p.text, vector=(0.0,) * 8)
        for p in points[:1]
    ]
    documents = [
        DocumentRecord(r.id, r.source_name, r.mime_type, r.size_bytes, embedding_model="old-model")
        for r in rows
    ]

    report = audit_embeddings(
        zeroed + points[1:],
        total_points=4,
        expected_dimension=8,
        expected_model="new-model",
        documents=documents,
    )

    assert finding(report, "zero_vectors").count == 1
    assert finding(report, "model_mismatch").count == 2
    assert report_json(report)["document_models"] == {"old-model": 2}
