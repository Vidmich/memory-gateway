"""The arithmetic of a connector-wide index audit (task 103, SPEC §6.6). Pure.

Task 20's Compare shows how *one* document is cut. This module answers the question
Compare cannot: how the **connector** is cut, over every point in the live collection, and
whether the vectors that came back from the provider are the vectors of that text. Nothing
here reads a store or calls a provider — :mod:`app.services.index_auditor` scrolls the
collection and hands the records in — so every finding below is a function of a list and
can be planted in a test.

Two reports, and the difference between them is what the input carries.

**The chunking report** is computed from payloads alone: token counts, text, sections,
fingerprints. It produces the histogram Compare left out, the five numbers from
:func:`~app.services.chunking_preview.distribution` over the whole index, and a list of
**findings**, each a retrieval defect with a count and the documents behind it. It is per
format as well as overall, because a repository connector's Markdown and its lockfiles have
different healthy shapes and averaging them hides both.

**The embedding report** needs the vectors, or at least a bounded sample of them, plus the
answers to a few searches the auditor ran against the live index. It checks the things a
provider can get silently wrong: the width, padding returned as vectors, a sample that
re-embeds to something else than what is stored, and whether a chunk's nearest neighbour is
its own document.

Every finding has a **severity**, and the rule for red is stated beside each one rather than
tuned in a table nobody reads: red means "retrieval is wrong for a material share of this
index", amber means "worth a look", and the dashboard's degraded list shows only red.

A word on what is *not* measured here. Near-duplicate detection by cosine lives in the
embedding report, where the vectors are; the chunking report finds the exact duplicates by
text hash, which is the common case and is free.
"""

from __future__ import annotations

import hashlib
import math
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.schemas.connector_config import ChunkingConfig, effective
from app.services.chunking_preview import CEILING_FRACTION, Distribution, _at
from app.services.filetypes import FORMAT_KINDS, format_label
from app.services.summarization import KIND_SOURCE, KIND_SUMMARY
from app.services.vector_store import cosine

CHUNKING = "chunking"
EMBEDDING = "embedding"

RED = "red"
AMBER = "amber"
GREEN = "green"
SEVERITY_ORDER = {GREEN: 0, AMBER: 1, RED: 2}

#: A chunk under this many tokens is a fragment: a heading with nothing under it, a table
#: cell, a line of a changelog. It embeds to something and matches nothing usefully.
CHUNK_FLOOR_TOKENS = 40
#: A chunk this far *over* the ceiling was not cut by the splitter at all — one unbroken
#: token run, a minified file, a table the boundary rules could not enter.
OVER_CEILING_FACTOR = 1.5
#: How many documents a finding names. The count is exact; the list is the way in.
FINDING_DOCUMENTS = 10
#: Buckets in the histogram, across ``[0, chunk_size x OVER_CEILING_FACTOR]``.
HISTOGRAM_BUCKETS = 12
#: The chunk-count outlier rule: a document whose chunks-per-kilobyte is this many times
#: the connector's median, either way, was cut very differently from its neighbours.
OUTLIER_FACTOR = 3.0
#: Below this many documents a median means nothing.
OUTLIER_MIN_DOCUMENTS = 5
#: Characters per token, for the size-based expectations. The same crude constant the
#: reindex estimate uses; a factor of two here does not move a factor-of-three rule.
CHARS_PER_TOKEN = 4

#: Formats where a chunk that begins mid-sentence is a defect. Code, data and spreadsheets
#: have no sentences to begin in the middle of.
PROSE_KINDS = frozenset({"markdown", "text", "html", "pdf", "docx", "pptx"})

#: Cosine at or above which two vectors are the same vector, allowing for float noise.
IDENTICAL_COSINE = 0.9999
#: The work item's number: near-duplicates across documents.
DUPLICATE_COSINE = 0.98
#: A norm under this is a zero vector with rounding on it.
ZERO_NORM = 1e-6
#: Drift: a re-embedded sample that agrees with the stored vector to this cosine is the
#: same model behaving the same way.
HEALTHY_DRIFT = 0.95
#: Below this share of chunks having their own document as nearest neighbour, the index
#: is either degenerate or the corpus is one subject — the report says which it thinks.
LOW_AGREEMENT = 0.7

_SPACE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PointRecord:
    """One point's payload, flattened. What a scroll page becomes."""

    id: str
    document_id: str
    chunk_index: int
    token_count: int
    text: str
    kind: str = KIND_SOURCE
    section: str | None = None
    fingerprint: str | None = None
    tokenizer: str | None = None
    #: Present only when the scroll asked for vectors.
    vector: tuple[float, ...] = ()

    @classmethod
    def of(
        cls, point_id: str, payload: Mapping[str, Any], vector: Sequence[float] = ()
    ) -> PointRecord:
        text = str(payload.get("text", ""))
        tokens = payload.get("token_count")
        return cls(
            id=point_id,
            document_id=str(payload.get("document_id", "")),
            chunk_index=int(payload.get("chunk_index", 0) or 0),
            # A point written before `token_count` existed is sized from its text.
            token_count=int(tokens) if tokens is not None else max(1, len(text) // CHARS_PER_TOKEN),
            text=text,
            kind=KIND_SUMMARY if payload.get("kind") == KIND_SUMMARY else KIND_SOURCE,
            section=(str(payload["page_or_section"]) if payload.get("page_or_section") else None),
            # Task 104's structured fingerprint where the point has one; task 20's digest
            # for a point written before it, so an old index still audits as one cutting.
            fingerprint=(
                str(payload.get("index_fingerprint") or payload.get("chunk_fingerprint") or "")
                or None
            ),
            tokenizer=str(payload["tokenizer"]) if payload.get("tokenizer") else None,
            vector=tuple(float(value) for value in vector),
        )


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    """What the audit needs from a ``documents`` row: how to name it and classify it."""

    id: str
    source_name: str
    mime_type: str | None
    size_bytes: int
    fingerprint: str | None = None
    embedding_model: str | None = None

    @property
    def kind(self) -> str:
        return format_label(self.mime_type or "")


# ---------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FindingDocument:
    id: str
    source_name: str
    count: int


@dataclass(frozen=True, slots=True)
class Finding:
    """One defect, counted, with the documents behind it and the sentence for the screen."""

    code: str
    severity: str
    title: str
    count: int
    detail: str
    documents: tuple[FindingDocument, ...] = ()
    #: How many distinct documents are behind ``count`` — the list above is the top few.
    document_count: int = 0
    #: Where the finding's fix lives: ``compare`` (a badly cut file opens Compare with it
    #: preselected), ``reindex`` (the connector's own), ``platform`` (the embedding model
    #: itself), or ``none``.
    action: str = "none"


@dataclass(frozen=True, slots=True)
class HistogramBucket:
    lower: int
    upper: int
    count: int


@dataclass(frozen=True, slots=True)
class Histogram:
    """Token counts, bucketed to the connector's ``chunk_size``. The last bucket is open."""

    bucket_tokens: int
    buckets: tuple[HistogramBucket, ...]


@dataclass(frozen=True, slots=True)
class FormatReport:
    kind: str
    points: int
    documents: int
    chunk_size: int
    distribution: Distribution
    histogram: Histogram
    findings: tuple[Finding, ...]


@dataclass(frozen=True, slots=True)
class ChunkingReport:
    points: int
    #: Source chunks only. Summary points (task 102) are counted apart: they are not cut.
    summary_points: int
    documents: int
    #: Documents in the table that have no point at all — indexed rows whose vectors are
    #: gone, or rows that never indexed. Reported, not judged.
    documents_without_points: int
    chunk_size: int
    distribution: Distribution
    histogram: Histogram
    formats: tuple[FormatReport, ...]
    findings: tuple[Finding, ...]
    #: Distinct ``index_fingerprint`` values and how many points carry each.
    fingerprints: Mapping[str, int]

    @property
    def severity(self) -> str:
        return worst(self.findings)


@dataclass(frozen=True, slots=True)
class Neighbour:
    """What the auditor found when it searched with one stored vector: the nearest point
    that is not the point itself."""

    point_id: str
    document_id: str
    neighbour_id: str | None
    neighbour_document_id: str | None
    score: float | None


@dataclass(frozen=True, slots=True)
class DriftSample:
    point_id: str
    document_id: str
    cosine: float


@dataclass(frozen=True, slots=True)
class Norms:
    min: float
    median: float
    p95: float
    max: float


@dataclass(frozen=True, slots=True)
class Agreement:
    """Intra-document agreement: of the sampled chunks, how many have their own document
    as nearest neighbour."""

    sampled: int
    agreed: int
    #: Mean similarity to the nearest *other-document* chunk. High means the corpus is one
    #: subject, and low agreement is then the corpus rather than the index.
    cross_document_similarity: float | None
    worst: tuple[FindingDocument, ...]

    @property
    def rate(self) -> float | None:
        return self.agreed / self.sampled if self.sampled else None


@dataclass(frozen=True, slots=True)
class Drift:
    sampled: int
    mean: float
    min: float
    p5: float
    #: Share of the sample under ``HEALTHY_DRIFT``.
    below: float
    #: What the shape says: ``healthy``, ``bimodal`` (two models in the collection),
    #: ``offset`` (one model, changed underneath), or ``low`` (something else).
    shape: str
    model: str


@dataclass(frozen=True, slots=True)
class EmbeddingReport:
    points: int
    #: How many points the vector checks actually looked at. Bounded; see the auditor.
    scanned: int
    expected_dimension: int
    dimensions: Mapping[int, int]
    expected_model: str
    #: ``documents.embedding_model`` values and their counts.
    document_models: Mapping[str, int]
    norms: Norms | None
    zero_vectors: int
    identical_vectors: int
    agreement: Agreement | None
    drift: Drift | None
    findings: tuple[Finding, ...]

    @property
    def severity(self) -> str:
        return worst(self.findings)


def worst(findings: Iterable[Finding]) -> str:
    level = GREEN
    for finding in findings:
        if SEVERITY_ORDER[finding.severity] > SEVERITY_ORDER[level]:
            level = finding.severity
    return level


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------


def audit_chunking(
    points: Sequence[PointRecord],
    documents: Sequence[DocumentRecord],
    config: ChunkingConfig,
) -> ChunkingReport:
    """The whole-index chunking report.

    ``documents`` classifies and names; ``points`` is what is actually in the index. A
    point whose document is not in the table is still counted — under ``other``, named by
    its id — because a point with no row is a finding in its own right that task 17's
    sweeper reports, and hiding it here would make the two disagree.
    """
    by_id = {document.id: document for document in documents}
    sources = [point for point in points if point.kind == KIND_SOURCE]
    summaries = len(points) - len(sources)

    per_kind: dict[str, list[PointRecord]] = defaultdict(list)
    for point in sources:
        document = by_id.get(point.document_id)
        per_kind[document.kind if document else "other"].append(point)

    formats = tuple(
        _format_report(kind, per_kind[kind], by_id, effective(config, kind))
        for kind in FORMAT_KINDS
        if per_kind.get(kind)
    )
    # The overall findings are the per-format ones merged by code, plus the two that only
    # mean something across formats: duplicates across documents and mixed fingerprints.
    merged = _merge_findings(finding for report in formats for finding in report.findings)
    merged.extend(_cross_format_findings(sources, by_id))
    merged.sort(key=lambda finding: (-SEVERITY_ORDER[finding.severity], -finding.count))

    present = {point.document_id for point in sources}
    return ChunkingReport(
        points=len(sources),
        summary_points=summaries,
        documents=len(present),
        documents_without_points=sum(1 for document in documents if document.id not in present),
        chunk_size=config.chunk_size,
        distribution=_distribution(sources, config.chunk_size),
        histogram=histogram(sources, config.chunk_size),
        formats=formats,
        findings=tuple(merged),
        fingerprints=dict(Counter(point.fingerprint or "" for point in sources)),
    )


def _format_report(
    kind: str,
    points: list[PointRecord],
    by_id: Mapping[str, DocumentRecord],
    config: ChunkingConfig,
) -> FormatReport:
    findings = [
        finding
        for finding in (
            _short_chunks(points, by_id, kind),
            _single_chunk_documents(points, by_id, kind),
            _at_ceiling(points, by_id, config, kind),
            _over_ceiling(points, by_id, config, kind),
            _mid_sentence(points, by_id, kind),
            _outliers(points, by_id, config, kind),
        )
        if finding is not None
    ]
    return FormatReport(
        kind=kind,
        points=len(points),
        documents=len({point.document_id for point in points}),
        chunk_size=config.chunk_size,
        distribution=_distribution(points, config.chunk_size),
        histogram=histogram(points, config.chunk_size),
        findings=tuple(findings),
    )


def _distribution(points: Sequence[PointRecord], chunk_size: int) -> Distribution:
    """:func:`chunking_preview.distribution` over payloads rather than chunks.

    The same percentile function and the same ceiling rule, so a whole-index number here
    and a one-document number in Compare are the same statistic — the acceptance criterion
    asserts they agree over the same points.
    """
    if not points:
        return Distribution(0, 0, 0, 0, 0, 0, 0)
    sizes = sorted(point.token_count for point in points)
    ceiling = int(chunk_size * CEILING_FRACTION)
    ordered = sorted(points, key=lambda point: (point.document_id, point.chunk_index))
    last_of_document = {
        document: max(p.chunk_index for p in group) for document, group in _group(ordered).items()
    }
    return Distribution(
        chunks=len(points),
        min_tokens=sizes[0],
        median_tokens=_at(sizes, 50),
        p95_tokens=_at(sizes, 95),
        max_tokens=sizes[-1],
        at_ceiling=sum(1 for size in sizes if size >= ceiling),
        # Compare skips a document's last chunk, which ends where the document ends. The
        # same rule per document here.
        mid_sentence=sum(
            1
            for point in ordered
            if point.chunk_index != last_of_document[point.document_id]
            and not point.text.rstrip().endswith(tuple(".!?\"')]"))
        ),
    )


def histogram(points: Sequence[PointRecord], chunk_size: int) -> Histogram:
    """Token counts in ``HISTOGRAM_BUCKETS`` buckets up to 1.5 x ``chunk_size``, the last
    bucket open. Bucketed to the ceiling rather than to the data so two connectors with
    the same setting draw on the same axis."""
    width = max(1, math.ceil(chunk_size * OVER_CEILING_FACTOR / HISTOGRAM_BUCKETS))
    counts = [0] * HISTOGRAM_BUCKETS
    for point in points:
        counts[min(HISTOGRAM_BUCKETS - 1, point.token_count // width)] += 1
    return Histogram(
        bucket_tokens=width,
        buckets=tuple(
            HistogramBucket(lower=index * width, upper=(index + 1) * width, count=count)
            for index, count in enumerate(counts)
        ),
    )


def _short_chunks(
    points: Sequence[PointRecord], by_id: Mapping[str, DocumentRecord], kind: str
) -> Finding | None:
    short = [point for point in points if point.token_count < CHUNK_FLOOR_TOKENS]
    if not short:
        return None
    share = len(short) / len(points)
    return Finding(
        code="short_chunks",
        # A tenth of the index being fragments is retrieval returning fragments.
        severity=RED if share >= 0.10 else AMBER,
        title=f"{len(short)} chunks under {CHUNK_FLOOR_TOKENS} tokens",
        count=len(short),
        detail=(
            f"{_percent(share)} of the {kind} chunks are fragments — a heading with nothing "
            "under it, a table cell, a changelog line. Each embeds to something and answers "
            "nothing. A larger min_chunk_size, or respect_boundaries, usually merges them."
        ),
        documents=_top_documents(short, by_id),
        document_count=len({point.document_id for point in short}),
        action="compare",
    )


def _single_chunk_documents(
    points: Sequence[PointRecord], by_id: Mapping[str, DocumentRecord], kind: str
) -> Finding | None:
    groups = _group(points)
    singles = [document for document, group in groups.items() if len(group) == 1]
    if not singles:
        return None
    share = len(singles) / len(groups)
    return Finding(
        code="single_chunk_documents",
        severity=AMBER,
        title=f"{len(singles)} documents are a single chunk",
        count=len(singles),
        detail=(
            f"{_percent(share)} of the {kind} documents fit in one chunk. Fine for a short "
            "file; for a long one it means the ceiling never applied and the whole document "
            "is one vector, which matches a question about any part of it equally badly."
        ),
        documents=tuple(
            FindingDocument(id=document, source_name=_name(by_id, document), count=1)
            for document in singles[:FINDING_DOCUMENTS]
        ),
        document_count=len(singles),
        action="compare",
    )


def _at_ceiling(
    points: Sequence[PointRecord],
    by_id: Mapping[str, DocumentRecord],
    config: ChunkingConfig,
    kind: str,
) -> Finding | None:
    ceiling = int(config.chunk_size * CEILING_FRACTION)
    hits = [point for point in points if ceiling <= point.token_count <= config.chunk_size]
    if not hits:
        return None
    share = len(hits) / len(points)
    # Under `fixed` the ceiling is the strategy; under anything else a majority at the
    # ceiling means the strategy is not earning its cost.
    severity = GREEN if config.strategy == "fixed" or share < 0.5 else AMBER
    return Finding(
        code="at_ceiling",
        severity=severity,
        title=f"{len(hits)} chunks cut at the {config.chunk_size}-token ceiling",
        count=len(hits),
        detail=(
            f"{_percent(share)} of the {kind} chunks were decided by chunk_size rather than "
            f"by a boundary under the {config.strategy} strategy."
            + (
                " That is what fixed does."
                if config.strategy == "fixed"
                else " More than half means the size limit is doing the cutting."
            )
        ),
        documents=_top_documents(hits, by_id),
        document_count=len({point.document_id for point in hits}),
        action="compare",
    )


def _over_ceiling(
    points: Sequence[PointRecord],
    by_id: Mapping[str, DocumentRecord],
    config: ChunkingConfig,
    kind: str,
) -> Finding | None:
    limit = int(config.chunk_size * OVER_CEILING_FACTOR)
    over = [point for point in points if point.token_count > limit]
    if not over:
        return None
    return Finding(
        code="over_ceiling",
        # Any at all: a chunk the splitter never should have produced, and one a provider
        # with an input limit truncates silently.
        severity=RED,
        title=f"{len(over)} chunks over {limit} tokens",
        count=len(over),
        detail=(
            f"These {kind} chunks are far over chunk_size: an unbroken run the boundary "
            "rules could not enter, a minified file, a table. An embedding provider that "
            "truncates its input embeds only the head of each, and the rest is unsearchable."
        ),
        documents=_top_documents(over, by_id),
        document_count=len({point.document_id for point in over}),
        action="compare",
    )


def _mid_sentence(
    points: Sequence[PointRecord], by_id: Mapping[str, DocumentRecord], kind: str
) -> Finding | None:
    if kind not in PROSE_KINDS:
        return None
    starts = [
        point for point in points if point.chunk_index > 0 and _starts_mid_sentence(point.text)
    ]
    if not starts:
        return None
    share = len(starts) / len(points)
    return Finding(
        code="mid_sentence_starts",
        severity=RED if share >= 0.25 else AMBER,
        title=f"{len(starts)} chunks begin mid-sentence",
        count=len(starts),
        detail=(
            f"{_percent(share)} of the {kind} chunks open with the tail of a sentence the "
            "previous chunk owns. respect_boundaries, or a strategy that cuts at sentences, "
            "removes these."
        ),
        documents=_top_documents(starts, by_id),
        document_count=len({point.document_id for point in starts}),
        action="compare",
    )


def _starts_mid_sentence(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped:
        return False
    first = stripped[0]
    # A lowercase letter is the tail of a sentence. Digits, punctuation and headings are
    # not a verdict either way.
    return first.isalpha() and first.islower()


def _outliers(
    points: Sequence[PointRecord],
    by_id: Mapping[str, DocumentRecord],
    config: ChunkingConfig,
    kind: str,
) -> Finding | None:
    """Documents whose chunk count is an outlier for their size.

    Chunks per kilobyte, against the format's median. A document three times denser than
    its neighbours was cut into fragments; one three times sparser was barely cut at all.
    Either way it is a document Compare should be opened on.
    """
    groups = _group(points)
    ratios: dict[str, float] = {}
    for document, group in groups.items():
        row = by_id.get(document)
        if row is None or row.size_bytes <= 0:
            continue
        ratios[document] = len(group) / (row.size_bytes / 1024)
    if len(ratios) < OUTLIER_MIN_DOCUMENTS:
        return None
    median = statistics.median(ratios.values())
    if median <= 0:
        return None
    outliers = [
        document
        for document, ratio in ratios.items()
        if ratio > median * OUTLIER_FACTOR or ratio < median / OUTLIER_FACTOR
    ]
    if not outliers:
        return None
    return Finding(
        code="chunk_count_outliers",
        severity=AMBER,
        title=f"{len(outliers)} documents cut unlike the rest",
        count=len(outliers),
        detail=(
            f"Chunks per kilobyte more than {OUTLIER_FACTOR:g}x the {kind} median either way. "
            f"The median is {median:.1f} chunks per KB under chunk_size {config.chunk_size}."
        ),
        documents=tuple(
            FindingDocument(
                id=document, source_name=_name(by_id, document), count=len(groups[document])
            )
            for document in sorted(
                outliers, key=lambda d: abs(math.log(ratios[d] / median)), reverse=True
            )[:FINDING_DOCUMENTS]
        ),
        document_count=len(outliers),
        action="compare",
    )


def _cross_format_findings(
    points: Sequence[PointRecord], by_id: Mapping[str, DocumentRecord]
) -> list[Finding]:
    findings = []
    duplicates = _duplicates(points, by_id)
    if duplicates is not None:
        findings.append(duplicates)
    mixed = _mixed_fingerprints(points, by_id)
    if mixed is not None:
        findings.append(mixed)
    return findings


def _duplicates(
    points: Sequence[PointRecord], by_id: Mapping[str, DocumentRecord]
) -> Finding | None:
    """Exact duplicates across documents, by normalised text hash.

    Two documents that share a chunk — a licence header, a boilerplate paragraph, the same
    file uploaded twice under two names — put two identical vectors in the index, and
    retrieval's dedupe cannot see it: that filter removes neighbours within a document.
    """
    by_hash: dict[str, list[PointRecord]] = defaultdict(list)
    for point in points:
        if point.token_count < 8:
            # Two empty-ish chunks matching each other is not the finding.
            continue
        by_hash[text_hash(point.text)].append(point)
    pairs = 0
    involved: list[PointRecord] = []
    for group in by_hash.values():
        documents = {point.document_id for point in group}
        if len(documents) < 2:
            continue
        pairs += len(documents) * (len(documents) - 1) // 2
        involved.extend(group)
    if not pairs:
        return None
    share = len(involved) / len(points) if points else 0.0
    return Finding(
        code="duplicate_chunks",
        severity=RED if share >= 0.05 else AMBER,
        title=f"{pairs} near-duplicate pairs across documents",
        count=pairs,
        detail=(
            f"{len(involved)} chunks ({_percent(share)}) have an identical twin in another "
            "document. A question they answer returns both and spends two slots of doc_top_k "
            "on one paragraph; the same file under two names is the usual cause."
        ),
        documents=_top_documents(involved, by_id),
        document_count=len({point.document_id for point in involved}),
        action="none",
    )


def _mixed_fingerprints(
    points: Sequence[PointRecord], by_id: Mapping[str, DocumentRecord]
) -> Finding | None:
    counts = Counter(point.fingerprint or "" for point in points)
    if len(counts) < 2:
        return None
    majority, _ = counts.most_common(1)[0]
    minority = [point for point in points if (point.fingerprint or "") != majority]
    return Finding(
        code="mixed_fingerprints",
        # Amber, not red: retrieval works across a mixed index, it just ranks two cuttings
        # against each other. Task 104 owns the fix; this report names it.
        severity=AMBER,
        title=f"{len(counts)} chunk fingerprints in one connector",
        count=len(minority),
        detail=(
            f"{len(minority)} chunks were cut under a configuration other than the one most "
            "of the connector uses — the connector is part-way through a reindex, or one was "
            "never run after a change. Reprocess it so every document is cut the same way."
        ),
        documents=_top_documents(minority, by_id),
        document_count=len({point.document_id for point in minority}),
        action="reindex",
    )


def _merge_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Per-format findings with the same code, combined into one connector-wide finding."""
    groups: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        groups[finding.code].append(finding)
    merged = []
    for code, group in groups.items():
        first = max(group, key=lambda f: f.count)
        count = sum(f.count for f in group)
        documents = sorted(
            (document for f in group for document in f.documents),
            key=lambda d: -d.count,
        )[:FINDING_DOCUMENTS]
        merged.append(
            Finding(
                code=code,
                severity=worst(group),
                title=_retitle(first.title, first.count, count),
                count=count,
                detail=first.detail if len(group) == 1 else _combined_detail(group),
                documents=tuple(documents),
                document_count=sum(f.document_count for f in group),
                action=first.action,
            )
        )
    return merged


def _retitle(title: str, old: int, new: int) -> str:
    return title.replace(str(old), str(new), 1) if old != new else title


def _combined_detail(group: Sequence[Finding]) -> str:
    return " ".join(f.detail for f in sorted(group, key=lambda f: -f.count)[:2])


# ---------------------------------------------------------------------------
# embeddings
# ---------------------------------------------------------------------------


def audit_embeddings(
    scanned: Sequence[PointRecord],
    *,
    total_points: int,
    expected_dimension: int,
    expected_model: str,
    documents: Sequence[DocumentRecord],
    neighbours: Sequence[Neighbour] = (),
    drift: Sequence[DriftSample] = (),
) -> EmbeddingReport:
    """The embedding sanity report over what the auditor could look at.

    ``scanned`` are the points whose vectors were pulled down — bounded by the auditor —
    and every count below is over them; ``total_points`` says how much of the collection
    that was. ``neighbours`` are the answers to the searches the auditor ran with a sample
    of stored vectors; ``drift`` the cosines between a re-embedded sample and what is
    stored. Both are optional: an audit with neither still checks the width, the norms and
    the padding.
    """
    by_id = {document.id: document for document in documents}
    with_vectors = [point for point in scanned if point.vector]

    dimensions = Counter(len(point.vector) for point in with_vectors)
    norms = [_norm(point.vector) for point in with_vectors]
    zero = [point for point, norm in zip(with_vectors, norms, strict=True) if norm < ZERO_NORM]
    identical = _identical(with_vectors)
    agreement = _agreement(neighbours, by_id) if neighbours else None
    drifted = _drift(drift, expected_model) if drift else None
    document_models = Counter(
        document.embedding_model or "" for document in documents if document.embedding_model
    )

    findings: list[Finding] = []
    if dimensions and set(dimensions) != {expected_dimension}:
        wrong = sum(count for width, count in dimensions.items() if width != expected_dimension)
        findings.append(
            Finding(
                code="dimension_mismatch",
                severity=RED,
                title=f"{wrong} vectors are not {expected_dimension} wide",
                count=wrong,
                detail=(
                    "Stored widths: "
                    + ", ".join(f"{width} x {count}" for width, count in sorted(dimensions.items()))
                    + f". The platform embedding model produces {expected_dimension}; a search "
                    "vector of one width against a collection of another either errors or "
                    "ranks garbage."
                ),
                action="platform",
            )
        )
    if zero:
        share = len(zero) / len(with_vectors)
        findings.append(
            Finding(
                code="zero_vectors",
                severity=RED if share >= 0.01 else AMBER,
                title=f"{len(zero)} zero vectors",
                count=len(zero),
                detail=(
                    f"{_percent(share)} of the scanned vectors have no magnitude. A provider "
                    "returned padding — a rate limit swallowed mid-batch, an empty input — "
                    "and these chunks match everything and nothing."
                ),
                documents=_top_documents(zero, by_id),
                document_count=len({point.document_id for point in zero}),
                action="reindex",
            )
        )
    if identical:
        share = len(identical) / len(with_vectors)
        findings.append(
            Finding(
                code="identical_vectors",
                severity=RED if share >= 0.05 else AMBER,
                title=f"{len(identical)} vectors are identical to another",
                count=len(identical),
                detail=(
                    f"{_percent(share)} of the scanned vectors are exact copies of another "
                    "vector for different text. A provider that truncates its input embeds the "
                    "same head for every chunk of a file it could not read whole; a batch that "
                    "returned one vector for all its inputs looks the same."
                ),
                documents=_top_documents(identical, by_id),
                document_count=len({point.document_id for point in identical}),
                action="reindex",
            )
        )
    if norms and len(norms) > 1:
        ordered = sorted(norm for norm in norms if norm >= ZERO_NORM)
        if ordered and ordered[-1] / ordered[0] > 1.5:
            findings.append(
                Finding(
                    code="norm_spread",
                    severity=AMBER,
                    title="vector norms are not uniform",
                    count=len(ordered),
                    detail=(
                        f"Norms range from {ordered[0]:.3f} to {ordered[-1]:.3f}. A normalising "
                        "provider returns unit vectors; a spread this wide means two providers "
                        "or two settings wrote into one collection, and cosine ranks them "
                        "against each other."
                    ),
                    action="reindex",
                )
            )
    if agreement is not None:
        rate = agreement.rate or 0.0
        if rate < LOW_AGREEMENT:
            similar = (agreement.cross_document_similarity or 0.0) >= 0.9
            findings.append(
                Finding(
                    code="low_agreement",
                    severity=AMBER if similar else RED,
                    title=(
                        f"{_percent(rate)} of chunks have their own document as nearest neighbour"
                    ),
                    count=agreement.sampled - agreement.agreed,
                    detail=(
                        (
                            "The documents themselves are near-identical — the nearest "
                            "other-document chunk scores "
                            f"{agreement.cross_document_similarity:.2f} on average — so a "
                            "chunk's closest neighbour is honestly in another "
                            "file. That can be fine: a corpus of one subject, or the same file "
                            "under several names."
                        )
                        if similar
                        else (
                            "A chunk's nearest neighbour should mostly be a chunk of the same "
                            "document. When it is not, and the documents are not alike, the "
                            "vectors are not describing the text — check the drift and "
                            "identical-vector findings."
                        )
                    ),
                    documents=agreement.worst,
                    document_count=len(agreement.worst),
                    action="none",
                )
            )
        duplicates = [n for n in neighbours if _cross_duplicate(n)]
        if duplicates:
            findings.append(
                Finding(
                    code="near_duplicate_vectors",
                    severity=AMBER,
                    title=f"{len(duplicates)} sampled chunks have a near-identical twin elsewhere",
                    count=len(duplicates),
                    detail=(
                        f"Cosine {DUPLICATE_COSINE} or above to a chunk of another document, in "
                        f"a sample of {len(neighbours)}. The chunking report finds the exact "
                        "copies; these are the rewordings and the near-copies."
                    ),
                    documents=_top_documents(
                        [PointRecord(n.point_id, n.document_id, 0, 0, "") for n in duplicates],
                        by_id,
                    ),
                    document_count=len({n.document_id for n in duplicates}),
                    action="none",
                )
            )
    if drifted is not None and drifted.shape != "healthy":
        findings.append(
            Finding(
                code="drift",
                severity=RED,
                title=f"a re-embedded sample agrees with the index at {drifted.mean:.2f}",
                count=round(drifted.below * drifted.sampled),
                detail=_drift_detail(drifted),
                action="platform",
            )
        )
    if document_models and set(document_models) != {expected_model}:
        others = {name: count for name, count in document_models.items() if name != expected_model}
        stale = sum(others.values())
        findings.append(
            Finding(
                code="model_mismatch",
                severity=AMBER,
                title=f"{stale} documents were embedded with another model",
                count=stale,
                detail=(
                    "Their rows say "
                    + ", ".join(f"{name} x {count}" for name, count in sorted(others.items()))
                    + f"; the platform model is {expected_model}. A finding, not a fix: the "
                    "platform reindex is what moves them."
                ),
                action="platform",
            )
        )

    findings.sort(key=lambda finding: (-SEVERITY_ORDER[finding.severity], -finding.count))
    return EmbeddingReport(
        points=total_points,
        scanned=len(with_vectors),
        expected_dimension=expected_dimension,
        dimensions=dict(dimensions),
        expected_model=expected_model,
        document_models=dict(document_models),
        norms=_norms(norms),
        zero_vectors=len(zero),
        identical_vectors=len(identical),
        agreement=agreement,
        drift=drifted,
        findings=tuple(findings),
    )


def _cross_duplicate(neighbour: Neighbour) -> bool:
    return (
        neighbour.neighbour_document_id is not None
        and neighbour.neighbour_document_id != neighbour.document_id
        and (neighbour.score or 0.0) >= DUPLICATE_COSINE
    )


def _identical(points: Sequence[PointRecord]) -> list[PointRecord]:
    """Points whose vector is an exact copy of another point's, for different text."""
    by_vector: dict[str, list[PointRecord]] = defaultdict(list)
    for point in points:
        by_vector[_vector_key(point.vector)].append(point)
    found = []
    for group in by_vector.values():
        if len(group) < 2 or len({text_hash(p.text) for p in group}) < 2:
            # Same text, same vector is the duplicate-chunk finding, not this one.
            continue
        found.extend(group)
    return found


def _vector_key(vector: Sequence[float]) -> str:
    rounded = ",".join(f"{value:.6f}" for value in vector)
    return hashlib.blake2b(rounded.encode("ascii"), digest_size=16).hexdigest()


def _agreement(neighbours: Sequence[Neighbour], by_id: Mapping[str, DocumentRecord]) -> Agreement:
    agreed = 0
    cross: list[float] = []
    misses: Counter[str] = Counter()
    for neighbour in neighbours:
        if neighbour.neighbour_document_id is None:
            continue
        if neighbour.neighbour_document_id == neighbour.document_id:
            agreed += 1
        else:
            misses[neighbour.document_id] += 1
            if neighbour.score is not None:
                cross.append(neighbour.score)
    return Agreement(
        sampled=len(neighbours),
        agreed=agreed,
        cross_document_similarity=statistics.fmean(cross) if cross else None,
        worst=tuple(
            FindingDocument(id=document, source_name=_name(by_id, document), count=count)
            for document, count in misses.most_common(FINDING_DOCUMENTS)
        ),
    )


def _drift(samples: Sequence[DriftSample], model: str) -> Drift:
    values = sorted(sample.cosine for sample in samples)
    below = sum(1 for value in values if value < HEALTHY_DRIFT) / len(values)
    mean = statistics.fmean(values)
    if below == 0.0 or (mean >= HEALTHY_DRIFT and below < 0.05):
        shape = "healthy"
    elif 0.2 <= below <= 0.8:
        # Part of the sample agrees and part does not: two models in the collection.
        shape = "bimodal"
    elif below > 0.8 and (values[-1] - values[0]) < 0.2:
        # None of it agrees and all of it disagrees by about the same amount: one model,
        # changed underneath the same id.
        shape = "offset"
    else:
        shape = "low"
    return Drift(
        sampled=len(values),
        mean=mean,
        min=values[0],
        p5=values[max(0, math.ceil(0.05 * len(values)) - 1)],
        below=below,
        shape=shape,
        model=model,
    )


def _drift_detail(drift: Drift) -> str:
    if drift.shape == "bimodal":
        return (
            f"{_percent(drift.below)} of the sample re-embeds to something else than what is "
            f"stored and the rest agrees: two embedding models are in this collection. A "
            "platform reindex makes it one."
        )
    if drift.shape == "offset":
        return (
            f"The whole sample disagrees with the index by about the same amount (mean "
            f"{drift.mean:.2f}, min {drift.min:.2f}). The provider changed something "
            f"underneath {drift.model}: same name, different vectors. Reindex, and expect to "
            "again the next time it happens."
        )
    return (
        f"Mean cosine {drift.mean:.2f} between the stored vectors and a fresh embedding of the "
        f"same text with {drift.model}; {_percent(drift.below)} of the sample is under "
        f"{HEALTHY_DRIFT}. The index is not what this model would build today."
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def text_hash(text: str) -> str:
    """Whitespace-normalised, case-folded. Two chunks that differ by a line break are the
    same chunk to a reader and nearly the same vector to a model."""
    normalised = _SPACE.sub(" ", text).strip().casefold()
    return hashlib.blake2b(normalised.encode("utf-8"), digest_size=16).hexdigest()


def _group(points: Iterable[PointRecord]) -> dict[str, list[PointRecord]]:
    groups: dict[str, list[PointRecord]] = defaultdict(list)
    for point in points:
        groups[point.document_id].append(point)
    return groups


def _top_documents(
    points: Sequence[PointRecord], by_id: Mapping[str, DocumentRecord]
) -> tuple[FindingDocument, ...]:
    counts = Counter(point.document_id for point in points)
    return tuple(
        FindingDocument(id=document, source_name=_name(by_id, document), count=count)
        for document, count in counts.most_common(FINDING_DOCUMENTS)
    )


def _name(by_id: Mapping[str, DocumentRecord], document_id: str) -> str:
    row = by_id.get(document_id)
    return row.source_name if row is not None else document_id


def _percent(share: float) -> str:
    return f"{share * 100:.0f}%"


def _norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def _norms(values: Sequence[float]) -> Norms | None:
    if not values:
        return None
    ordered = sorted(values)
    return Norms(
        min=ordered[0],
        median=statistics.median(ordered),
        p95=ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)],
        max=ordered[-1],
    )


def drift_cosines(
    stored: Sequence[PointRecord], fresh: Sequence[Sequence[float]]
) -> list[DriftSample]:
    """Pair a sample's stored vectors with their fresh embeddings, in order."""
    return [
        DriftSample(
            point_id=point.id,
            document_id=point.document_id,
            cosine=cosine(point.vector, vector),
        )
        for point, vector in zip(stored, fresh, strict=True)
    ]


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def report_json(report: ChunkingReport | EmbeddingReport) -> dict[str, Any]:
    """The row's ``report`` column: plain values, keyed the way the API returns them."""
    if isinstance(report, ChunkingReport):
        return {
            "kind": CHUNKING,
            "points": report.points,
            "summary_points": report.summary_points,
            "documents": report.documents,
            "documents_without_points": report.documents_without_points,
            "chunk_size": report.chunk_size,
            "distribution": _distribution_json(report.distribution),
            "histogram": _histogram_json(report.histogram),
            "formats": [
                {
                    "kind": entry.kind,
                    "points": entry.points,
                    "documents": entry.documents,
                    "chunk_size": entry.chunk_size,
                    "distribution": _distribution_json(entry.distribution),
                    "histogram": _histogram_json(entry.histogram),
                    "findings": [_finding_json(f) for f in entry.findings],
                }
                for entry in report.formats
            ],
            "findings": [_finding_json(f) for f in report.findings],
            "fingerprints": dict(report.fingerprints),
            "severity": report.severity,
        }
    return {
        "kind": EMBEDDING,
        "points": report.points,
        "scanned": report.scanned,
        "expected_dimension": report.expected_dimension,
        "dimensions": {str(width): count for width, count in report.dimensions.items()},
        "expected_model": report.expected_model,
        "document_models": dict(report.document_models),
        "norms": (
            {
                "min": report.norms.min,
                "median": report.norms.median,
                "p95": report.norms.p95,
                "max": report.norms.max,
            }
            if report.norms
            else None
        ),
        "zero_vectors": report.zero_vectors,
        "identical_vectors": report.identical_vectors,
        "agreement": (
            {
                "sampled": report.agreement.sampled,
                "agreed": report.agreement.agreed,
                "rate": report.agreement.rate,
                "cross_document_similarity": report.agreement.cross_document_similarity,
                "worst": [_document_json(d) for d in report.agreement.worst],
            }
            if report.agreement
            else None
        ),
        "drift": (
            {
                "sampled": report.drift.sampled,
                "mean": report.drift.mean,
                "min": report.drift.min,
                "p5": report.drift.p5,
                "below": report.drift.below,
                "shape": report.drift.shape,
                "model": report.drift.model,
            }
            if report.drift
            else None
        ),
        "findings": [_finding_json(f) for f in report.findings],
        "severity": report.severity,
    }


def _distribution_json(distribution: Distribution) -> dict[str, int]:
    return {
        "chunks": distribution.chunks,
        "min_tokens": distribution.min_tokens,
        "median_tokens": distribution.median_tokens,
        "p95_tokens": distribution.p95_tokens,
        "max_tokens": distribution.max_tokens,
        "at_ceiling": distribution.at_ceiling,
        "mid_sentence": distribution.mid_sentence,
    }


def _histogram_json(histogram: Histogram) -> dict[str, Any]:
    return {
        "bucket_tokens": histogram.bucket_tokens,
        "buckets": [
            {"lower": bucket.lower, "upper": bucket.upper, "count": bucket.count}
            for bucket in histogram.buckets
        ],
    }


def _finding_json(finding: Finding) -> dict[str, Any]:
    return {
        "code": finding.code,
        "severity": finding.severity,
        "title": finding.title,
        "count": finding.count,
        "detail": finding.detail,
        "documents": [_document_json(d) for d in finding.documents],
        "document_count": finding.document_count,
        "action": finding.action,
    }


def _document_json(document: FindingDocument) -> dict[str, Any]:
    return {"id": document.id, "source_name": document.source_name, "count": document.count}


__all__ = [
    "AMBER",
    "CHUNKING",
    "CHUNK_FLOOR_TOKENS",
    "DUPLICATE_COSINE",
    "EMBEDDING",
    "GREEN",
    "HEALTHY_DRIFT",
    "RED",
    "Agreement",
    "ChunkingReport",
    "DocumentRecord",
    "Drift",
    "DriftSample",
    "EmbeddingReport",
    "Finding",
    "FindingDocument",
    "FormatReport",
    "Histogram",
    "HistogramBucket",
    "Neighbour",
    "Norms",
    "PointRecord",
    "audit_chunking",
    "audit_embeddings",
    "drift_cosines",
    "histogram",
    "report_json",
    "text_hash",
    "worst",
]
