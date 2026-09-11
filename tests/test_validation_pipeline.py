"""Task 103 end to end, over the in-memory stack: audits over a real index, and evaluation
runs through the real retrieval path.

The acceptance criteria this file carries:

* an evaluation run's retrieved chunks for each question are **identical** to Try
  retrieval's for the same question and configuration — asserted, not assumed;
* two runs across a reindex of the connector: the second re-anchors chunk labels by text,
  reports how many it could not, and the diff names the fingerprint change;
* an item imported from the log with task 100 citations arrives pre-labelled and
  unverified, and verifying it moves it between the two reported columns;
* generated items are marked, a run including them says so, and the model call is a
  ledger row with ``purpose: evaluation``;
* intra-document agreement is high on distinct subjects and reported low — with the
  sentence about why that can be fine — on near-identical documents;
* audits and runs write nothing to the index.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.errors import NotFound, Validation
from app.db.models import Transcript
from app.schemas.connector_config import ChunkingConfig
from app.services.connectors import ConnectorPatch
from app.services.embeddings import HashEmbedder
from app.services.evaluation import MAX_ITEMS_PER_RUN
from app.services.evaluation_service import ItemDraft
from app.services.index_audit import AMBER, GREEN, RED
from tests.auth_support import make_organization
from tests.connector_support import DIMENSION, ConnectorFixture, build_connectors
from tests.gateway_support import make_gateway_row
from tests.monitoring_support import make_log_row
from tests.validation_support import ValidationFixture, build_validation_fixture

LEAVE = (
    "Annual leave. Everyone gets twenty-five days of annual leave, plus public holidays, "
    "and can carry five days into the next year with their manager's agreement. Leave is "
    "booked in the portal at least two weeks ahead. Unused leave beyond the carried five "
    "days lapses at the end of December. Part-time colleagues accrue leave pro rata. "
    "Parental leave is a separate policy with its own handbook page. "
)
EXPENSES = (
    "Expenses. Receipts are uploaded to the portal within thirty days of the spend. "
    "Anything over ten euros needs a receipt; anything over five hundred needs approval "
    "in advance from the budget holder. Mileage is reimbursed at the government rate. "
    "Client entertainment is capped at eighty euros a head and needs the names of the "
    "guests. Reimbursement lands with the next payroll run after approval. "
)
TRAVEL = (
    "Travel. Flights are booked through the agency, economy under six hours and premium "
    "above. Hotels are capped by city; the cap list is on the intranet. Trains are "
    "preferred to flights under four hours. Travel insurance covers every employee "
    "automatically; keep the policy number from the intranet in your phone. Visas are "
    "arranged by the agency with three weeks' notice. "
)
ZYNTHORP = (
    "The Zynthorp QX-4471 stabiliser ships with a nineteen-month warranty and is serviced "
    "only at the Utrecht depot. Warranty claims quote the serial number and the depot "
    "returns the unit within ten working days. Out-of-warranty repairs are quoted first. "
)


@pytest.fixture
def connectors() -> ConnectorFixture:
    return build_connectors(make_organization())


@pytest.fixture
def validation(connectors: ConnectorFixture) -> ValidationFixture:
    return build_validation_fixture(connectors)


async def small_chunks(connectors: ConnectorFixture, size: int = 60) -> None:
    """Cut every document into several chunks, so a document has neighbours of its own."""
    await connectors.service.update_connector(
        connectors.actor,
        connectors.connector.id,
        ConnectorPatch(chunking={"chunk_size": size, "overlap": 0, "strategy": "recursive"}),
    )


async def upload(connectors: ConnectorFixture, *files: tuple[str, str], repeat: int = 4) -> None:
    await connectors.upload(*((name, (text * repeat).encode()) for name, text in files))
    await connectors.run_jobs()


def snapshot(connectors: ConnectorFixture) -> dict[str, Any]:
    return copy.deepcopy(connectors.vectors.collections)


async def gateway_for(validation: ValidationFixture, **memory: Any) -> uuid.UUID:
    connectors = validation.connectors
    row = make_gateway_row(
        connectors.organization,
        slug=f"support-{uuid.uuid4().hex[:6]}",
        memory_config={
            "connector_ids": [str(connectors.connector.id)],
            "doc_min_score": 0.0,
            **memory,
        },
    )
    connectors.database.add_gateway(row)
    return row.id


async def first_chunk(validation: ValidationFixture, name: str) -> Any:
    """The first source chunk of the document with this name, from the live index."""
    connectors = validation.connectors
    async with connectors.store.begin(connectors.actor.scope) as transaction:
        documents = await transaction.documents(
            connectors.connector.id, after=None, limit=50, status="indexed"
        )
    document = next(row for row in documents if row.source_name == name)
    chunks = await connectors.vectors.chunks(connectors.organization_id, document.id)
    sources = [chunk for chunk in chunks if chunk.payload.get("kind", "source") == "source"]
    return document, sources[0]


# ---------------------------------------------------------------------------
# chunking audit
# ---------------------------------------------------------------------------


async def test_the_chunking_audit_reports_over_the_whole_index_and_names_the_fragments(
    validation: ValidationFixture,
) -> None:
    connectors = validation.connectors
    await small_chunks(connectors)
    changelog = "\n".join(f"- fix {n}" for n in range(60))
    await upload(
        connectors,
        ("leave.md", LEAVE),
        ("expenses.md", EXPENSES),
        ("CHANGELOG.md", changelog),
        repeat=3,
    )
    before = snapshot(connectors)

    audit = await validation.auditor.start(connectors.actor, connectors.connector.id, "chunking")
    assert audit.status == "running"
    await validation.run_jobs()

    status = await validation.auditor.status(connectors.actor, connectors.connector.id)
    assert status.chunking is not None
    assert status.chunking.status == "succeeded", status.chunking.error
    report = status.chunking.report
    expected = await connectors.vectors.count(
        connectors.organization_id, connector_id=connectors.connector.id
    )
    assert report["points"] == expected
    assert report["documents"] == 3
    assert report["chunk_size"] == 60
    assert sum(b["count"] for b in report["histogram"]["buckets"]) == expected
    assert [entry["kind"] for entry in report["formats"]] == ["markdown"]
    assert report["fingerprints"] and len(report["fingerprints"]) == 1
    # The audit wrote nothing.
    assert snapshot(connectors) == before


async def test_a_second_start_while_one_is_running_returns_the_running_row(
    validation: ValidationFixture,
) -> None:
    connectors = validation.connectors
    first = await validation.auditor.start(connectors.actor, connectors.connector.id, "chunking")
    second = await validation.auditor.start(connectors.actor, connectors.connector.id, "chunking")

    assert second.id == first.id
    assert len(connectors.queue.pending) == 1


async def test_an_audit_of_an_empty_connector_succeeds_with_nothing_in_it(
    validation: ValidationFixture,
) -> None:
    connectors = validation.connectors
    await validation.auditor.start(connectors.actor, connectors.connector.id, "embedding")
    await validation.run_jobs()

    status = await validation.auditor.status(connectors.actor, connectors.connector.id)
    assert status.embedding is not None and status.embedding.status == "succeeded"
    assert status.embedding.report["points"] == 0
    assert status.embedding.severity == GREEN
    assert status.drift_estimate.points == 0


async def test_the_audit_kind_is_checked_and_drift_belongs_to_embeddings(
    validation: ValidationFixture,
) -> None:
    connectors = validation.connectors
    with pytest.raises(Validation):
        await validation.auditor.start(connectors.actor, connectors.connector.id, "vibes")
    with pytest.raises(Validation):
        await validation.auditor.start(
            connectors.actor, connectors.connector.id, "chunking", drift_sample=10
        )
    with pytest.raises(NotFound):
        await validation.auditor.start(connectors.actor, uuid.uuid4(), "chunking")


# ---------------------------------------------------------------------------
# embedding audit
# ---------------------------------------------------------------------------


async def test_distinct_subjects_agree_with_their_own_document_and_a_fresh_sample_matches(
    validation: ValidationFixture,
) -> None:
    connectors = validation.connectors
    await small_chunks(connectors)
    await upload(connectors, ("leave.md", LEAVE), ("expenses.md", EXPENSES), ("travel.md", TRAVEL))
    before = snapshot(connectors)

    await validation.auditor.start(
        connectors.actor, connectors.connector.id, "embedding", drift_sample=20
    )
    await validation.run_jobs()

    status = await validation.auditor.status(connectors.actor, connectors.connector.id)
    assert status.embedding is not None
    report = status.embedding.report
    assert status.embedding.status == "succeeded", status.embedding.error
    assert report["expected_dimension"] == DIMENSION
    assert report["dimensions"] == {str(DIMENSION): report["scanned"]}
    assert report["agreement"]["rate"] > 0.9
    assert report["drift"]["shape"] == "healthy"
    assert report["drift"]["sampled"] == min(20, report["scanned"])
    assert report["zero_vectors"] == 0
    assert status.embedding.severity == GREEN
    assert snapshot(connectors) == before


async def test_near_identical_documents_report_low_agreement_and_say_why_that_can_be_fine(
    validation: ValidationFixture,
) -> None:
    connectors = validation.connectors
    await small_chunks(connectors)
    # The same handbook under four names, with one word changed so the texts are not
    # byte-identical: every chunk's nearest neighbour is its twin in another file.
    await upload(
        connectors,
        *((f"copy-{n}.md", LEAVE.replace("Everyone", f"Everyone{n}")) for n in range(4)),
    )

    await validation.auditor.start(connectors.actor, connectors.connector.id, "embedding")
    await validation.run_jobs()

    status = await validation.auditor.status(connectors.actor, connectors.connector.id)
    assert status.embedding is not None
    report = status.embedding.report
    assert report["agreement"]["rate"] < 0.7
    low = next(f for f in report["findings"] if f["code"] == "low_agreement")
    assert low["severity"] == AMBER
    assert "That can be fine" in low["detail"]


class Drifted(HashEmbedder):
    """The same model name, different vectors: what a provider changing something under
    the same id looks like from here."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = await super().embed(texts)
        return [vector[1:] + vector[:1] for vector in vectors]


async def test_a_provider_that_changed_underneath_the_model_is_caught_by_the_drift_check(
    connectors: ConnectorFixture,
) -> None:
    await small_chunks(connectors)
    await upload(connectors, ("leave.md", LEAVE), ("expenses.md", EXPENSES))
    validation = build_validation_fixture(connectors)
    # The auditor's embedder is what the platform serves *now*; the index was built by
    # the fixture's. Same name, same width, different vectors.
    validation.auditor._embedder = Drifted(dimension=DIMENSION, model="hash-bow")

    await validation.auditor.start(
        connectors.actor, connectors.connector.id, "embedding", drift_sample=30
    )
    await validation.run_jobs()

    status = await validation.auditor.status(connectors.actor, connectors.connector.id)
    assert status.embedding is not None
    drift = next(f for f in status.embedding.report["findings"] if f["code"] == "drift")
    assert drift["severity"] == RED
    assert status.embedding.severity == RED
    alerts = await validation.auditor.alerts(connectors.actor)
    assert [(a.connector_id, a.kind) for a in alerts] == [(connectors.connector.id, "embedding")]
    assert alerts[0].connector_name == connectors.connector.name


async def test_the_drift_estimate_says_what_the_button_would_spend(
    validation: ValidationFixture,
) -> None:
    connectors = validation.connectors
    await small_chunks(connectors)
    await upload(connectors, ("leave.md", LEAVE))

    status = await validation.auditor.status(connectors.actor, connectors.connector.id)

    points = await connectors.vectors.count(
        connectors.organization_id, connector_id=connectors.connector.id
    )
    assert status.drift_estimate.points == points
    assert status.drift_estimate.sample == min(points, 100)
    assert status.drift_estimate.tokens == status.drift_estimate.sample * 60


# ---------------------------------------------------------------------------
# evaluation runs
# ---------------------------------------------------------------------------


async def labelled_set(
    validation: ValidationFixture, gateway_id: uuid.UUID, *, chunk_index: int | None = None
) -> Any:
    """A set with one labelled question per document and one negative.

    Labelled the way the screen labels: the chunk Try retrieval puts first for the
    question is the one a person marks relevant, so the label is the top-ranked chunk of
    the right document. With ``chunk_index`` the label is that chunk instead and the
    question is its own text — for the tests that need to know exactly which chunk a
    label named, past chunk zero, whose id a recut keeps.
    """
    connectors = validation.connectors
    created = await validation.service.create_set(connectors.actor, gateway_id, name="Handbook")
    for name, question in (
        ("leave.md", "how many days of annual leave and carry into next year"),
        ("expenses.md", "receipts uploaded within thirty days reimbursement payroll"),
        ("zynthorp.md", "Zynthorp QX-4471 warranty Utrecht depot serial"),
    ):
        document, _ = await first_chunk(validation, name)
        if chunk_index is None:
            preview = await validation.preview.try_retrieval(
                connectors.actor, gateway_id, query=question
            )
            top = preview.chunks[0].chunk
            assert top.document_id == str(document.id), (name, top.source_name)
            chunk_id, asked = top.id, question
        else:
            stored = await connectors.vectors.chunks(connectors.organization_id, document.id)
            sources = [chunk for chunk in stored if chunk.payload.get("kind", "source") == "source"]
            chosen = sources[min(chunk_index, len(sources) - 1)]
            chunk_id, asked = chosen.id, chosen.text
        await validation.service.add_item(
            connectors.actor,
            created.set.id,
            ItemDraft(
                question=asked,
                relevant=[{"chunk_id": chunk_id, "document_id": str(document.id)}],
            ),
        )
    await validation.service.add_item(
        connectors.actor,
        created.set.id,
        ItemDraft(question="what is the office wifi password", relevant=[]),
    )
    return created.set


async def finished_run(validation: ValidationFixture, set_id: uuid.UUID, **patch: Any) -> Any:
    connectors = validation.connectors
    run = await validation.service.start_run(connectors.actor, set_id, memory_config=patch or None)
    await validation.run_jobs()
    return await validation.service.run(connectors.actor, run.id)


async def test_a_run_retrieves_exactly_what_try_retrieval_retrieves(
    validation: ValidationFixture,
) -> None:
    """The parity criterion. Same question, same configuration — including an unsaved
    patch — and the chunk ids and the budget's verdict are identical."""
    connectors = validation.connectors
    await small_chunks(connectors)
    await upload(
        connectors, ("leave.md", LEAVE), ("expenses.md", EXPENSES), ("zynthorp.md", ZYNTHORP)
    )
    gateway_id = await gateway_for(validation, doc_top_k=4, doc_max_tokens=90)
    evaluation_set = await labelled_set(validation, gateway_id)
    before = snapshot(connectors)

    run = await finished_run(validation, evaluation_set.id, doc_top_k=3)

    assert run.status == "succeeded", run.error
    assert run.total_items == 4 and run.completed_items == 4
    assert run.patch == {"doc_top_k": 3}
    for result in run.results:
        preview = await validation.preview.try_retrieval(
            connectors.actor,
            gateway_id,
            query=result["question"],
            memory_config={"doc_top_k": 3},
        )
        assert [r["chunk_id"] for r in result["retrieved"]] == [c.chunk.id for c in preview.chunks]
        assert [r["injected"] for r in result["retrieved"]] == [c.injected for c in preview.chunks]
    assert snapshot(connectors) == before


async def test_a_run_scores_a_labelled_fixture_the_way_a_person_would(
    validation: ValidationFixture,
) -> None:
    connectors = validation.connectors
    await small_chunks(connectors)
    await upload(
        connectors, ("leave.md", LEAVE), ("expenses.md", EXPENSES), ("zynthorp.md", ZYNTHORP)
    )
    gateway_id = await gateway_for(validation, doc_top_k=6)
    evaluation_set = await labelled_set(validation, gateway_id)

    run = await finished_run(validation, evaluation_set.id)

    metrics = run.metrics
    assert metrics["k"] == 6
    assert metrics["all"]["items"] == 4
    assert metrics["all"]["negatives"] == 1
    assert metrics["all"]["chunk"]["items"] == 3
    # Each question is worded from its own passage, so the labelled chunk comes back.
    assert metrics["all"]["chunk"]["recall"] == 1.0
    assert metrics["all"]["document"]["hit_rate"] == 1.0
    assert metrics["all"]["chunk"]["mrr"] > 0.5
    # Manual items are verified: the two columns agree.
    assert metrics["verified"]["items"] == 4
    assert metrics["warnings"] == []
    zynthorp = next(r for r in run.results if r["question"].startswith("Zynthorp"))
    assert zynthorp["chunk"]["first_rank"] == 1
    assert run.snapshot["embedding_model"] == "hash-bow"
    assert str(connectors.connector.id) in run.snapshot["connectors"]
    assert run.config["doc_top_k"] == 6


async def test_a_run_survives_a_reindex_by_re_anchoring_labels_and_the_diff_names_the_recut(
    validation: ValidationFixture,
) -> None:
    connectors = validation.connectors
    await small_chunks(connectors, 60)
    # Each file once, so every chunk's text is its own: a label has to be found again by
    # *its* text, not by a twin paragraph's.
    await upload(
        connectors,
        ("leave.md", LEAVE),
        ("expenses.md", EXPENSES),
        ("zynthorp.md", ZYNTHORP),
        repeat=1,
    )
    gateway_id = await gateway_for(validation)
    evaluation_set = await labelled_set(validation, gateway_id, chunk_index=1)
    first = await finished_run(validation, evaluation_set.id)
    old_fingerprints = set(run_fingerprints(first, connectors.connector.id))

    # A recut: bigger chunks, new ids for every point.
    await small_chunks(connectors, 200)
    await connectors.service.reindex_connector(connectors.actor, connectors.connector.id)
    await connectors.run_jobs()
    # And one document gone entirely, so one label has nowhere to go.
    zynthorp, _ = await first_chunk(validation, "zynthorp.md")
    await connectors.service.delete_document(connectors.actor, zynthorp.id)

    second = await finished_run(validation, evaluation_set.id)

    assert second.status == "succeeded", second.error
    assert second.metrics["reanchored"] == 2
    assert second.metrics["unanchored"] == 1
    # The re-anchored labels found the bigger chunks; the items now point at them.
    detail = await validation.service.detail(connectors.actor, evaluation_set.id)
    leave = next(item for item in detail.items if "leave" in item.question.lower())
    live = {
        c.id
        for c in await connectors.vectors.chunks(
            connectors.organization_id, uuid.UUID(leave.relevant[0]["document_id"])
        )
    }
    assert all(label["chunk_id"] in live for label in leave.relevant)
    assert second.metrics["all"]["chunk"]["recall"] > 0

    diff = await validation.service.diff(connectors.actor, first.id, second.id)
    assert diff.before.id == first.id and diff.after.id == second.id
    assert set(run_fingerprints(second, connectors.connector.id)) != old_fingerprints
    assert any("reindexed between the runs" in change for change in diff.index_changes)
    assert diff.config_changes == {}


def run_fingerprints(run: Any, connector_id: uuid.UUID) -> dict[str, int]:
    return dict(run.snapshot["connectors"][str(connector_id)]["fingerprints"])


async def test_an_unsaved_patch_is_what_the_diff_names_when_the_index_did_not_move(
    validation: ValidationFixture,
) -> None:
    connectors = validation.connectors
    await small_chunks(connectors)
    await upload(
        connectors, ("leave.md", LEAVE), ("expenses.md", EXPENSES), ("zynthorp.md", ZYNTHORP)
    )
    gateway_id = await gateway_for(validation, doc_top_k=6)
    evaluation_set = await labelled_set(validation, gateway_id)

    loose = await finished_run(validation, evaluation_set.id)
    strict = await finished_run(validation, evaluation_set.id, doc_min_score=0.99)

    diff = await validation.service.diff(connectors.actor, strict.id, loose.id)
    assert diff.before.id == loose.id
    assert diff.config_changes == {"doc_min_score": (0.0, 0.99)}
    assert diff.index_changes == ()
    recall = next(delta for delta in diff.metrics if delta.name == "chunk recall")
    assert recall.before == 1.0 and recall.after == 0.0
    assert len(diff.lost) == 3 and diff.won == ()
    # The negative item is the one that improved: nothing above 0.99.
    assert strict.metrics["all"]["negatives_clean"] == 1


async def test_items_imported_from_the_log_arrive_labelled_by_the_citation_and_unverified(
    validation: ValidationFixture,
) -> None:
    connectors = validation.connectors
    await small_chunks(connectors)
    # Once each: the cited chunk has to be the only chunk with its text, or retrieval may
    # honestly return a twin under another id.
    await upload(connectors, ("leave.md", LEAVE), ("zynthorp.md", ZYNTHORP), repeat=1)
    gateway_id = await gateway_for(validation)
    document, chunk = await first_chunk(validation, "zynthorp.md")
    now = datetime.now(UTC)
    retrieved = [
        {
            "id": chunk.id,
            "score": 0.8,
            "document_id": str(document.id),
            "source_name": "zynthorp.md",
            "page_or_section": None,
            "chunk_index": 0,
            "injected": True,
        }
    ]
    for offset, (question, cited) in enumerate(
        (
            ("Zynthorp QX-4471 warranty Utrecht depot serial", [chunk.id]),
            ("zynthorp qx-4471 warranty utrecht depot serial!", [chunk.id]),  # a duplicate
            ("does the handbook cover pets in the office", []),
        )
    ):
        row = make_log_row(
            connectors.organization,
            gateway_id=gateway_id,
            created_at=now - timedelta(minutes=offset + 1),
            retrieved_chunk_ids=retrieved,
            cited_chunk_ids=cited,
        )
        connectors.database.request_logs[row.id] = row
        connectors.database.transcripts[row.id] = Transcript(
            request_log_id=row.id,
            created_at=row.created_at,
            organization_id=connectors.organization_id,
            request_body=[{"role": "user", "content": question}],
            assembled_prompt=None,
            response_body="…",
        )
    created = await validation.service.create_set(connectors.actor, gateway_id, name="Imported")

    result = await validation.service.import_from_log(
        connectors.actor, created.set.id, start=now - timedelta(hours=1), end=now
    )

    assert (result.imported, result.duplicates, result.labelled) == (2, 1, 1)
    detail = await validation.service.detail(connectors.actor, created.set.id)
    by_source = {item.source: item for item in detail.items}
    assert set(by_source) == {"citation", "log"}
    cited_item = by_source["citation"]
    assert not cited_item.verified
    assert cited_item.relevant[0]["chunk_id"] == chunk.id
    assert cited_item.relevant[0]["text"] == chunk.text
    assert not by_source["log"].relevant  # unlabelled, not negative by intent

    # Importing the same window again adds nothing.
    again = await validation.service.import_from_log(
        connectors.actor, created.set.id, start=now - timedelta(hours=1), end=now
    )
    assert again.imported == 0 and again.duplicates == 3

    # Unverified until somebody confirms; verifying moves the item between the columns.
    first = await finished_run(validation, created.set.id)
    assert first.metrics["all"]["chunk"]["items"] == 1
    assert first.metrics["verified"]["chunk"]["items"] == 0
    assert first.metrics["unverified"] == 2
    await validation.service.update_item(connectors.actor, cited_item.id, {"verified": True})
    second = await finished_run(validation, created.set.id)
    assert second.metrics["verified"]["chunk"]["items"] == 1
    assert second.metrics["verified"]["chunk"]["recall"] == 1.0


async def test_generated_items_are_marked_and_the_model_call_is_a_ledger_row(
    connectors: ConnectorFixture,
) -> None:
    connectors.database.add_model(connectors.summary_row)
    validation = build_validation_fixture(connectors)
    validation.question_model.queue(
        "How many days of annual leave do I get?",
        "Question: What is the receipt threshold for expenses?",
    )
    await small_chunks(connectors, 200)
    await upload(connectors, ("leave.md", LEAVE), ("expenses.md", EXPENSES))
    gateway_id = await gateway_for(validation)
    created = await validation.service.create_set(connectors.actor, gateway_id, name="Synthetic")

    result = await validation.service.generate(connectors.actor, created.set.id, count=2)

    assert result.generated == 2 and result.failed == 0
    assert result.tokens_in == 240 and result.tokens_out == 80
    assert result.model_name == connectors.summary_row.name
    detail = await validation.service.detail(connectors.actor, created.set.id)
    assert {item.source for item in detail.items} == {"generated"}
    assert all(not item.verified for item in detail.items)
    assert all(item.relevant[0]["text"] for item in detail.items)
    assert detail.counts.generated == 2
    ledger = connectors.runs()
    assert [row.purpose for row in ledger] == ["evaluation", "evaluation"]
    assert all(row.outcome == "succeeded" for row in ledger)
    # And the summarization panel does not count them as documents summarized.
    health = await connectors.summaries_health()
    assert health.documents == 0

    run = await finished_run(validation, created.set.id)
    assert run.metrics["generated"] == 2
    assert any("generated by a model" in warning for warning in run.metrics["warnings"])
    # The question was written from the answer: it finds the document. (The fixture
    # repeats its paragraphs, so which identical copy comes back is not a fact to assert.)
    assert run.metrics["all"]["document"]["hit_rate"] == 1.0


async def test_generation_refuses_when_nothing_resolves_a_model(
    validation: ValidationFixture,
) -> None:
    connectors = validation.connectors
    await small_chunks(connectors, 200)
    await upload(connectors, ("leave.md", LEAVE))
    gateway_id = await gateway_for(validation)
    created = await validation.service.create_set(connectors.actor, gateway_id, name="Synthetic")

    with pytest.raises(Validation, match="No model is configured"):
        await validation.service.generate(connectors.actor, created.set.id, count=1)
    assert connectors.runs() == []


async def test_a_label_naming_a_chunk_that_is_not_in_this_index_is_not_found(
    validation: ValidationFixture,
) -> None:
    """A chunk id from another organization — or one that never existed — is a 404 when the
    label is written, not a hit or a miss later."""
    connectors = validation.connectors
    await small_chunks(connectors)
    await upload(connectors, ("leave.md", LEAVE))
    gateway_id = await gateway_for(validation)
    created = await validation.service.create_set(connectors.actor, gateway_id, name="Owned")
    document, _ = await first_chunk(validation, "leave.md")

    stranger = build_connectors(make_organization(name="Globex", slug="globex"))
    await small_chunks(stranger)
    await upload(stranger, ("secret.md", ZYNTHORP))
    foreign = build_validation_fixture(stranger)
    foreign_document, foreign_chunk = await first_chunk(foreign, "secret.md")

    with pytest.raises(NotFound):
        await validation.service.add_item(
            connectors.actor,
            created.set.id,
            ItemDraft(
                question="what is the warranty",
                relevant=[{"chunk_id": foreign_chunk.id, "document_id": str(foreign_document.id)}],
            ),
        )
    with pytest.raises(NotFound):
        await validation.service.add_item(
            connectors.actor,
            created.set.id,
            ItemDraft(
                question="what is the warranty",
                relevant_document_ids=[foreign_document.id],
            ),
        )
    with pytest.raises(NotFound):
        await validation.service.add_item(
            connectors.actor,
            created.set.id,
            ItemDraft(
                question="what is the warranty",
                relevant=[{"chunk_id": "no-such-chunk", "document_id": str(document.id)}],
            ),
        )


async def test_a_run_of_an_empty_set_or_a_second_run_in_flight_is_refused(
    validation: ValidationFixture,
) -> None:
    from app.core.errors import Conflict

    connectors = validation.connectors
    gateway_id = await gateway_for(validation)
    created = await validation.service.create_set(connectors.actor, gateway_id, name="Empty")

    with pytest.raises(Validation, match="no items"):
        await validation.service.start_run(connectors.actor, created.set.id)

    await validation.service.add_item(
        connectors.actor, created.set.id, ItemDraft(question="anything at all?", relevant=[])
    )
    await validation.service.start_run(connectors.actor, created.set.id)
    with pytest.raises(Conflict):
        await validation.service.start_run(connectors.actor, created.set.id)


async def test_a_run_caps_the_items_it_takes(validation: ValidationFixture) -> None:
    connectors = validation.connectors
    gateway_id = await gateway_for(validation)
    created = await validation.service.create_set(connectors.actor, gateway_id, name="Big")
    for n in range(3):
        await validation.service.add_item(
            connectors.actor, created.set.id, ItemDraft(question=f"question {n}?", relevant=[])
        )

    run = await validation.service.start_run(connectors.actor, created.set.id)

    assert run.total_items == min(3, MAX_ITEMS_PER_RUN)
    assert MAX_ITEMS_PER_RUN == 500


async def test_the_gateways_own_connectors_are_the_only_ones_a_run_reads(
    validation: ValidationFixture,
) -> None:
    """The SPEC §5.3 check Try retrieval performs, performed by the run: a connector id in
    the patch that the gateway may not read is dropped, and the run's stored config says
    so."""
    connectors = validation.connectors
    await small_chunks(connectors)
    await upload(connectors, ("leave.md", LEAVE))
    gateway_id = await gateway_for(validation)
    created = await validation.service.create_set(connectors.actor, gateway_id, name="Scoped")
    await validation.service.add_item(
        connectors.actor, created.set.id, ItemDraft(question="annual leave days?", relevant=[])
    )

    run = await finished_run(
        validation,
        created.set.id,
        connector_ids=[str(connectors.connector.id), str(uuid.uuid4())],
    )

    assert run.config["connector_ids"] == [str(connectors.connector.id)]


def test_the_chunking_config_used_by_the_fixture_is_what_the_audit_reports() -> None:
    assert ChunkingConfig.load({"chunk_size": 60, "overlap": 0}).chunk_size == 60
