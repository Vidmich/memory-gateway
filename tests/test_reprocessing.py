"""Reprocessing status (task 104), end to end over the memory stack.

Staleness as a stored fact: a save marks the affected rows, the counts read the same on
every screen, a run re-ingests exactly its scope with exact counters, a dead worker's run
is continued, retrieval keeps answering and labels what it served, and the reconciliation
pass finds nothing to correct when everything else did its job.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.errors import NotFound, Validation
from app.core.tenancy import TenantScope
from app.schemas.connector_config import ChunkingConfig
from app.schemas.gateway_config import MemoryConfig
from app.schemas.openai import ChatMessage
from app.services import extraction
from app.services.connector_store import ReprocessScope
from app.services.connectors import ConnectorPatch
from app.services.extraction import ExtractionError
from app.services.retrieval import is_stale
from app.services.vector_store import ChunkPoint, point_id
from tests.auth_support import make_organization
from tests.connector_support import ConnectorFixture, build_connectors

PARAGRAPH = (
    "Expenses are reimbursed within thirty days once receipts are uploaded through the "
    "portal, and payroll settles the amount with the next salary run. "
)
HANDBOOK = (PARAGRAPH * 6).encode()
PDF_LIKE = (
    "Annual leave is twenty-five days plus public holidays; five days carry over with a "
    "manager's agreement, and unused days beyond that lapse at year end. " * 6
).encode()


@pytest.fixture
def fixture() -> ConnectorFixture:
    return build_connectors(make_organization())


async def small_chunks(fixture: ConnectorFixture, size: int = 60) -> Any:
    return await fixture.service.update_connector(
        fixture.actor,
        fixture.connector.id,
        ConnectorPatch(chunking={"chunk_size": size, "overlap": 0, "strategy": "recursive"}),
    )


async def statuses(fixture: ConnectorFixture) -> dict[str, str]:
    return {row.source_name: row.index_status for row in await fixture.documents()}


async def recall(fixture: ConnectorFixture, question: str, *, k: int = 6) -> Any:
    return await fixture.memory._retriever.documents(
        organization_id=fixture.organization_id,
        config=MemoryConfig(connector_ids=[fixture.connector.id], doc_top_k=k, doc_min_score=0.0),
        messages=[ChatMessage(role="user", content=question)],
    )


# ---------------------------------------------------------------------------
# staleness, stored and served
# ---------------------------------------------------------------------------


async def test_ingestion_records_a_structured_fingerprint_and_a_current_status(
    fixture: ConnectorFixture,
) -> None:
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")

    assert document.index_status == "current"
    assert document.index_fingerprint is not None and document.index_fingerprint.startswith("ch=")
    # Task 20's digest is still written this release (expand-contract).
    assert document.chunk_fingerprint is not None
    view = await fixture.service.get_connector(fixture.actor, fixture.connector.id)
    assert view.effective_fingerprints["markdown"] == document.index_fingerprint
    chunks = await fixture.points(document.id)
    assert {chunk.payload["index_fingerprint"] for chunk in chunks} == {document.index_fingerprint}
    assert {chunk.payload["format_kind"] for chunk in chunks} == {"markdown"}


async def test_a_chunking_change_marks_the_rows_stale_and_every_read_agrees(
    fixture: ConnectorFixture,
) -> None:
    """The acceptance criterion: the PATCH response, a GET, the list and the document
    table all say the same thing, because the fact is on the rows."""
    await fixture.ingest(("handbook.md", HANDBOOK), ("leave.md", PDF_LIKE))

    patched = await small_chunks(fixture)

    assert patched.reindex_required is True
    assert patched.reindex_formats == frozenset({"markdown"})
    assert patched.stale.stale == 2
    fetched = await fixture.service.get_connector(fixture.actor, fixture.connector.id)
    assert fetched.stale.stale == 2 and fetched.reindex_required is True
    listed = await fixture.service.list_connectors(fixture.actor)
    assert listed.items[0].stale.stale == 2 and listed.items[0].reindex_required is True
    page = await fixture.service.list_documents(fixture.actor, fixture.connector.id)
    assert {row.index_status for row in page.items} == {"stale"}
    assert {reason.code for reason in page.reasons.values()} == {"chunking"}
    assert all("settings changed" in reason.sentence for reason in page.reasons.values())


async def test_reverting_the_change_un_marks_them(fixture: ConnectorFixture) -> None:
    """The one UPDATE compares fingerprints rather than blindly marking, so undoing a
    save is a save like any other — and this one makes the rows current again."""
    await fixture.ingest(("handbook.md", HANDBOOK))
    original = {
        key: value
        for key, value in ChunkingConfig().model_dump().items()
        if key in ("strategy", "chunk_size", "overlap")
    }
    await small_chunks(fixture)
    assert (await statuses(fixture)) == {"handbook.md": "stale"}

    await fixture.service.update_connector(
        fixture.actor, fixture.connector.id, ConnectorPatch(chunking=original)
    )

    assert (await statuses(fixture)) == {"handbook.md": "current"}


async def test_a_per_format_override_marks_only_that_format(fixture: ConnectorFixture) -> None:
    await fixture.ingest(("handbook.md", HANDBOOK), ("notes.txt", PDF_LIKE))

    await fixture.service.update_connector(
        fixture.actor,
        fixture.connector.id,
        ConnectorPatch(chunking={"overrides": {"text": {"chunk_size": 400, "overlap": 40}}}),
    )

    assert (await statuses(fixture)) == {"handbook.md": "current", "notes.txt": "stale"}
    view = await fixture.service.get_connector(fixture.actor, fixture.connector.id)
    assert view.reindex_formats == frozenset({"text"})
    filtered = await fixture.service.list_documents(
        fixture.actor, fixture.connector.id, index_status="stale"
    )
    assert [row.source_name for row in filtered.items] == ["notes.txt"]


async def test_a_save_says_how_many_it_would_mark_before_saving(fixture: ConnectorFixture) -> None:
    await fixture.ingest(("handbook.md", HANDBOOK), ("notes.txt", PDF_LIKE))

    preview = await fixture.service.stale_preview(
        fixture.actor,
        fixture.connector.id,
        ConnectorPatch(chunking={"chunk_size": 400, "overlap": 40}),
    )
    nothing = await fixture.service.stale_preview(
        fixture.actor, fixture.connector.id, ConnectorPatch(description="renamed")
    )
    one = await fixture.service.stale_preview(
        fixture.actor,
        fixture.connector.id,
        ConnectorPatch(chunking={"overrides": {"text": {"chunk_size": 400, "overlap": 40}}}),
    )

    assert preview == {"markdown": 1, "text": 1}
    assert nothing == {}
    assert one == {"text": 1}
    assert (await statuses(fixture)) == {"handbook.md": "current", "notes.txt": "current"}


async def test_an_extractor_upgrade_marks_its_format_stale_and_the_rest_current(
    fixture: ConnectorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    await fixture.ingest(("handbook.md", HANDBOOK), ("notes.txt", PDF_LIKE))

    monkeypatch.setitem(extraction.EXTRACTION_VERSIONS, "text", 2)
    report = await fixture.reprocessor.reconcile()

    assert report.disagreements == 1
    assert (await statuses(fixture)) == {"handbook.md": "current", "notes.txt": "stale"}
    page = await fixture.service.list_documents(fixture.actor, fixture.connector.id)
    [reason] = page.reasons.values()
    assert reason.code == "extractor" and "upgraded" in reason.sentence


async def test_a_row_without_a_fingerprint_is_unrecorded_never_stale(
    fixture: ConnectorFixture,
) -> None:
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")
    document.index_fingerprint = None

    await small_chunks(fixture)
    report = await fixture.reprocessor.reconcile()

    assert report.disagreements == 0
    view = await fixture.service.get_connector(fixture.actor, fixture.connector.id)
    assert view.stale.stale == 0 and view.stale.unrecorded == 1
    assert view.reindex_required is False
    page = await fixture.service.list_documents(fixture.actor, fixture.connector.id)
    assert page.reasons[document.id].code == "unrecorded"
    assert page.items[0].index_status == "current"

    # The "reprocess unrecorded" scope takes it, and the run records a fingerprint.
    started = await fixture.reprocessor.start(
        fixture.actor, fixture.connector.id, scope=ReprocessScope(kind="unrecorded")
    )
    assert started.run.total == 1
    await fixture.run_jobs()
    assert (await fixture.document("handbook.md")).index_fingerprint is not None


async def test_the_reconciliation_pass_finds_no_disagreements_when_the_writers_did_their_job(
    fixture: ConnectorFixture,
) -> None:
    await fixture.ingest(("handbook.md", HANDBOOK), ("notes.txt", PDF_LIKE))
    await small_chunks(fixture)
    await fixture.reprocessor.start(fixture.actor, fixture.connector.id)
    await fixture.run_jobs()

    report = await fixture.reprocessor.reconcile()

    assert report.connectors == 1
    assert report.disagreements == 0
    assert report.stale_by_connector == {fixture.connector.id: 0}


async def test_the_reconciliation_pass_corrects_a_planted_disagreement(
    fixture: ConnectorFixture,
) -> None:
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")
    document.index_status = "stale"  # a writer that got it wrong

    report = await fixture.reprocessor.reconcile()

    assert report.disagreements == 1
    assert document.index_status == "current"


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


async def test_a_run_takes_the_stale_documents_and_leaves_the_current_ones(
    fixture: ConnectorFixture,
) -> None:
    await fixture.ingest(("handbook.md", HANDBOOK), ("notes.txt", PDF_LIKE))
    await fixture.service.update_connector(
        fixture.actor,
        fixture.connector.id,
        ConnectorPatch(chunking={"overrides": {"text": {"chunk_size": 400, "overlap": 40}}}),
    )
    before = (await fixture.document("handbook.md")).indexed_at

    started = await fixture.reprocessor.start(fixture.actor, fixture.connector.id)

    assert started.created is True
    run = started.run
    assert run.total == 1 and run.scope == "stale" and run.trigger == "chunking"
    assert run.estimated_tokens > 0
    assert (await statuses(fixture)) == {"handbook.md": "current", "notes.txt": "reprocessing"}
    view = await fixture.service.get_connector(fixture.actor, fixture.connector.id)
    assert view.reprocessing is not None and view.reprocessing.id == run.id
    assert view.stale.reprocessing == 1

    await fixture.run_jobs()

    finished = await fixture.reprocessor.run(fixture.actor, run.id)
    assert finished.status == "succeeded"
    assert (finished.done, finished.failed, finished.skipped) == (1, 0, 0)
    assert finished.spent_tokens > 0
    assert finished.finished_at is not None
    assert (await statuses(fixture)) == {"handbook.md": "current", "notes.txt": "current"}
    assert (await fixture.document("handbook.md")).indexed_at == before
    assert (
        await fixture.service.get_connector(fixture.actor, fixture.connector.id)
    ).reprocessing is None


async def test_one_run_per_connector_at_a_time(fixture: ConnectorFixture) -> None:
    await fixture.ingest(("handbook.md", HANDBOOK))
    await small_chunks(fixture)

    first = await fixture.reprocessor.start(fixture.actor, fixture.connector.id)
    second = await fixture.reprocessor.start(
        fixture.actor, fixture.connector.id, scope=ReprocessScope(kind="all")
    )

    assert second.created is False
    assert second.run.id == first.run.id
    await fixture.run_jobs()
    third = await fixture.reprocessor.start(
        fixture.actor, fixture.connector.id, scope=ReprocessScope(kind="all")
    )
    assert third.created is True and third.run.id != first.run.id


async def test_a_run_with_nothing_in_scope_finishes_at_once(fixture: ConnectorFixture) -> None:
    await fixture.ingest(("handbook.md", HANDBOOK))

    started = await fixture.reprocessor.start(fixture.actor, fixture.connector.id)

    assert started.run.total == 0
    assert started.run.status == "succeeded" and started.run.finished_at is not None
    assert started.run.trigger == "manual"
    assert fixture.queue.pending == []


async def test_a_bad_scope_or_format_is_refused(fixture: ConnectorFixture) -> None:
    with pytest.raises(Validation):
        await fixture.reprocessor.start(
            fixture.actor, fixture.connector.id, scope=ReprocessScope(kind="everything")
        )
    with pytest.raises(Validation):
        await fixture.reprocessor.start(
            fixture.actor,
            fixture.connector.id,
            scope=ReprocessScope(kind="formats", formats=frozenset({"parchment"})),
        )
    with pytest.raises(NotFound):
        await fixture.reprocessor.start(fixture.actor, uuid.uuid4())


async def test_planted_failures_and_missing_sources_finish_partial_and_retry_the_failures(
    fixture: ConnectorFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion with numbers: N documents, 3 planted failures, 2 sources
    gone from object storage → ``partial`` with exact counters, progress monotone, and
    **Retry failed** re-enqueues exactly the three."""
    names = [f"doc-{index:02d}.md" for index in range(12)]
    await fixture.ingest(*((name, HANDBOOK) for name in names))
    await small_chunks(fixture)

    broken = {"doc-01.md", "doc-05.md", "doc-09.md"}
    original = fixture.pipeline._extract

    async def extract(data: bytes, *, name: str, media_type: str) -> Any:
        if name in broken:
            raise ExtractionError("planted", reason="planted_failure")
        return await original(data, name=name, media_type=media_type)

    monkeypatch.setattr(fixture.pipeline, "_extract", extract)
    for gone in ("doc-02.md", "doc-07.md"):
        await fixture.objects.delete([(await fixture.document(gone)).source_uri])

    started = await fixture.reprocessor.start(fixture.actor, fixture.connector.id)
    assert started.run.total == 12

    seen: list[int] = []
    while fixture.queue.pending:
        await fixture.runner.run(fixture.queue.pending.pop(0))
        run = await fixture.reprocessor.run(fixture.actor, started.run.id)
        seen.append(run.settled)
    assert seen == sorted(seen) and seen[-1] == 12

    run = await fixture.reprocessor.run(fixture.actor, started.run.id)
    assert run.status == "partial"
    assert (run.done, run.failed, run.skipped) == (7, 3, 2)
    assert (await fixture.statuses())["doc-01.md"] == "failed"
    assert (await fixture.statuses())["doc-02.md"] == "failed"
    assert (await fixture.document("doc-02.md")).reason == "missing_object"

    broken.clear()
    retry = await fixture.reprocessor.retry_failed(fixture.actor, run.id)
    assert retry.created is True
    assert retry.run.scope == "failed" and retry.run.total == 3
    assert retry.run.trigger == run.trigger
    assert {
        fixture.database.documents[uuid.UUID(job.payload["document_id"])].source_name
        for job in fixture.queue.pending
    } == {"doc-01.md", "doc-05.md", "doc-09.md"}
    await fixture.run_jobs()
    assert (await fixture.reprocessor.run(fixture.actor, retry.run.id)).status == "succeeded"
    assert (await fixture.statuses())["doc-05.md"] == "indexed"


async def test_a_retry_of_a_run_still_going_is_refused(fixture: ConnectorFixture) -> None:
    await fixture.ingest(("handbook.md", HANDBOOK))
    await small_chunks(fixture)
    started = await fixture.reprocessor.start(fixture.actor, fixture.connector.id)

    with pytest.raises(Validation):
        await fixture.reprocessor.retry_failed(fixture.actor, started.run.id)


async def test_a_killed_worker_leaves_a_run_that_is_continued_from_its_counters(
    fixture: ConnectorFixture,
) -> None:
    """Three documents; the worker runs one and dies with two queued. The queue is lost
    with it. The reconciliation pass finds the run still owns two unfinished documents,
    re-enqueues exactly those under a key the queue has not seen, and the counters pick
    up at one rather than starting over."""
    await fixture.ingest(("a.md", HANDBOOK), ("b.md", HANDBOOK), ("c.md", HANDBOOK))
    await small_chunks(fixture)
    started = await fixture.reprocessor.start(fixture.actor, fixture.connector.id)
    assert len(fixture.queue.pending) == 3
    first_keys = {job.idempotency_key for job in fixture.queue.pending}

    await fixture.runner.run(fixture.queue.pending.pop(0))
    fixture.queue.pending.clear()  # the worker died; Redis lost what it held
    run = await fixture.reprocessor.run(fixture.actor, started.run.id)
    assert run.settled == 1 and run.finished_at is None

    too_soon = await fixture.reprocessor.continue_stalled()
    assert too_soon == (0, 0) and fixture.queue.pending == []

    later = datetime.now(UTC) + timedelta(hours=1)
    continued, closed = await fixture.reprocessor.continue_stalled(now=later)

    assert (continued, closed) == (1, 0)
    assert len(fixture.queue.pending) == 2
    assert {job.idempotency_key for job in fixture.queue.pending}.isdisjoint(first_keys)
    await fixture.run_jobs()
    run = await fixture.reprocessor.run(fixture.actor, started.run.id)
    assert run.status == "succeeded" and run.resumed == 1
    assert (run.done, run.total) == (3, 3)


async def test_a_stalled_run_with_nothing_left_is_closed_at_its_counters(
    fixture: ConnectorFixture,
) -> None:
    await fixture.ingest(("a.md", HANDBOOK), ("b.md", HANDBOOK))
    await small_chunks(fixture)
    started = await fixture.reprocessor.start(fixture.actor, fixture.connector.id)
    await fixture.runner.run(fixture.queue.pending.pop(0))
    fixture.queue.pending.clear()
    # The other document was deleted while the run waited.
    remaining = next(row for row in await fixture.documents() if row.status != "indexed")
    await fixture.service.delete_document(fixture.actor, remaining.id)

    continued, closed = await fixture.reprocessor.continue_stalled(
        now=datetime.now(UTC) + timedelta(hours=1)
    )

    assert (continued, closed) == (0, 1)
    run = await fixture.reprocessor.run(fixture.actor, started.run.id)
    assert run.status == "succeeded" and run.total == 1 and run.done == 1


async def test_a_manual_retry_of_a_document_a_run_owns_still_counts_for_the_run(
    fixture: ConnectorFixture,
) -> None:
    await fixture.ingest(("a.md", HANDBOOK))
    await small_chunks(fixture)
    started = await fixture.reprocessor.start(fixture.actor, fixture.connector.id)
    fixture.queue.pending.clear()
    document = await fixture.document("a.md")

    await fixture.service.reindex_document(fixture.actor, document.id)
    await fixture.run_jobs()

    run = await fixture.reprocessor.run(fixture.actor, started.run.id)
    assert run.status == "succeeded" and run.done == 1


async def test_the_reindex_alias_starts_the_same_tracked_run(fixture: ConnectorFixture) -> None:
    await fixture.ingest(("handbook.md", HANDBOOK), ("notes.txt", PDF_LIKE))
    await small_chunks(fixture)

    queued = await fixture.service.reindex_connector(
        fixture.actor, fixture.connector.id, formats=["text"]
    )

    assert queued == 1
    runs = await fixture.reprocessor.runs(fixture.actor, fixture.connector.id)
    assert len(runs) == 1 and runs[0].scope == "formats" and runs[0].formats == ["text"]


async def test_the_history_is_newest_first_with_who_and_what_it_cost(
    fixture: ConnectorFixture,
) -> None:
    await fixture.ingest(("handbook.md", HANDBOOK))
    await small_chunks(fixture)
    first = await fixture.reprocessor.start(fixture.actor, fixture.connector.id)
    await fixture.run_jobs()
    await small_chunks(fixture, 80)
    second = await fixture.reprocessor.start(fixture.actor, fixture.connector.id)
    await fixture.run_jobs()

    runs = await fixture.reprocessor.runs(fixture.actor, fixture.connector.id)

    assert [run.id for run in runs] == [second.run.id, first.run.id]
    assert all(run.requested_by == fixture.actor.user_id for run in runs)
    assert all(run.estimated_tokens > 0 and run.spent_tokens > 0 for run in runs)
    with pytest.raises(NotFound):
        await fixture.reprocessor.runs(fixture.actor, uuid.uuid4())


# ---------------------------------------------------------------------------
# retrieval while mixed
# ---------------------------------------------------------------------------


async def test_stale_chunks_are_still_retrieved_and_labelled(fixture: ConnectorFixture) -> None:
    await fixture.ingest(("handbook.md", HANDBOOK))
    fresh = await recall(fixture, "receipts uploaded through the portal reimbursed")
    assert fresh.chunks and not any(chunk.stale for chunk in fresh.chunks)

    await small_chunks(fixture)  # saved, not yet reprocessed

    mixed = await recall(fixture, "receipts uploaded through the portal reimbursed")
    assert mixed.chunks, "a connector mid-reprocess keeps answering"
    assert all(chunk.stale for chunk in mixed.chunks)
    entry = mixed.chunks[0].as_log_entry(injected=True)
    assert entry["stale"] is True

    await fixture.reprocessor.start(fixture.actor, fixture.connector.id)
    await fixture.run_jobs()
    after = await recall(fixture, "receipts uploaded through the portal reimbursed")
    assert after.chunks and not any(chunk.stale for chunk in after.chunks)
    assert "stale" not in after.chunks[0].as_log_entry(injected=True)


def test_the_stale_label_is_decided_from_the_payload() -> None:
    expected = {"c1": {"markdown": "ch=a;em=b;tk=c;sm=-;xv=1", "text": "ch=z;em=b;tk=c;sm=-;xv=1"}}
    current = {
        "connector_id": "c1",
        "format_kind": "markdown",
        "index_fingerprint": "ch=a;em=b;tk=c;sm=-;xv=1",
    }
    old = {**current, "index_fingerprint": "ch=old;em=b;tk=c;sm=-;xv=1"}
    assert is_stale(current, expected) is False
    assert is_stale(old, expected) is True
    # No format on the point: stale only if it matches none of the connector's.
    assert (
        is_stale({"connector_id": "c1", "index_fingerprint": "ch=z;em=b;tk=c;sm=-;xv=1"}, expected)
        is False
    )
    assert (
        is_stale({"connector_id": "c1", "index_fingerprint": "ch=q;em=b;tk=c;sm=-;xv=1"}, expected)
        is True
    )
    # Unrecorded, unknown connector, or nothing to compare against: never stale.
    assert is_stale({"connector_id": "c1"}, expected) is False
    assert is_stale({**old, "connector_id": "c9"}, expected) is False
    assert is_stale(old, None) is False


async def test_a_document_mid_replacement_never_returns_the_same_text_twice(
    fixture: ConnectorFixture,
) -> None:
    """The replacement window: the new cut is upserted before the old tail is deleted. A
    paragraph that did not move sits in the index twice, at two indexes under two
    fingerprints, and retrieval's dedupe collapses the pair."""
    await small_chunks(fixture)
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")
    stored = await fixture.points(document.id)
    assert len(stored) >= 3
    twin = stored[1]
    # An old point at a tail index carrying the same text as a surviving new one.
    ghost = ChunkPoint(
        id=point_id(document.id, len(stored) + 5),
        vector=list(
            fixture.vectors.collections[fixture.vectors.live(fixture.organization_id)][
                twin.id
            ].vector
        ),
        payload={
            **twin.payload,
            "chunk_index": len(stored) + 5,
            "index_fingerprint": "ch=old;em=x;tk=y;sm=-;xv=1",
        },
    )
    await fixture.vectors.upsert(fixture.organization_id, [ghost])

    found = await recall(fixture, twin.text, k=10)

    texts = [chunk.text for chunk in found.chunks]
    assert len(texts) == len(set(texts))
    assert twin.text in texts


async def test_replacing_a_document_upserts_before_it_deletes(fixture: ConnectorFixture) -> None:
    """Asserted on the order, not only on the end state: a reader between the two steps
    sees the new points, never an empty document."""
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")
    seen: list[tuple[str, int]] = []
    store = fixture.vectors
    original_upsert, original_replace = store.upsert, store.replace_document

    async def upsert(organization_id: uuid.UUID, points: Any) -> None:
        await original_upsert(organization_id, points)
        seen.append(("upsert", await store.count(organization_id, document_id=document.id)))

    async def replace(organization_id: uuid.UUID, document_id: uuid.UUID, points: Any) -> None:
        await original_replace(organization_id, document_id, points)
        seen.append(("replace", await store.count(organization_id, document_id=document.id)))

    store.upsert = upsert  # type: ignore[method-assign]
    store.replace_document = replace  # type: ignore[method-assign]
    await small_chunks(fixture)
    await fixture.reprocessor.start(fixture.actor, fixture.connector.id)
    await fixture.run_jobs()

    assert [step for step, _ in seen] == ["upsert", "replace"]
    assert all(count > 0 for _, count in seen)


# ---------------------------------------------------------------------------
# the platform reindex's runs
# ---------------------------------------------------------------------------


async def test_a_reindex_spawned_run_marks_reprocessing_then_current_and_adoption_moves_the_model(
    fixture: ConnectorFixture,
) -> None:
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")
    before = document.index_fingerprint
    assert before is not None
    reindex_run_id = uuid.uuid4()

    run = await fixture.reprocessor.open_for_reindex(
        organization_id=fixture.organization_id,
        connector_id=fixture.connector.id,
        reindex_run_id=reindex_run_id,
        documents=[document.id],
    )
    assert run.trigger == "embedding_model" and run.reindex_run_id == reindex_run_id
    assert document.index_status == "reprocessing" and document.status == "indexed"

    await fixture.reprocessor.settle_recut(
        organization_id=fixture.organization_id,
        run_id=run.id,
        document_id=document.id,
        outcome="done",
        tokens=12,
        chunk_count=4,
    )
    settled = await fixture.reprocessor.run(fixture.actor, run.id)
    assert settled.status == "succeeded" and settled.spent_tokens == 12
    assert document.index_status == "current" and document.chunk_count == 4
    assert [spawned.id for spawned in await fixture.reprocessor.spawned_by(reindex_run_id)] == [
        run.id
    ]

    await fixture.reprocessor.adopt_embedding_model(fixture.organization_id, "hash-next")

    assert document.embedding_model == "hash-next"
    assert document.index_fingerprint != before
    # The process still embeds with the old model, so against *its* expectation the row
    # now reads stale for the embedding model — which is exactly what a worker that has
    # not restarted would say, and the sentence names both models.
    page = await fixture.service.list_documents(fixture.actor, fixture.connector.id)
    assert page.reasons == {} or page.reasons[document.id].code == "embedding_model"


# ---------------------------------------------------------------------------
# where staleness is felt
# ---------------------------------------------------------------------------


async def test_a_gateway_reading_a_stale_connector_says_so(fixture: ConnectorFixture) -> None:
    from app.services.gateway_store import MemoryGatewayStore

    await fixture.ingest(("handbook.md", HANDBOOK))
    store = MemoryGatewayStore(fixture.database)
    scope = TenantScope.of_organization(fixture.organization_id)
    async with store.begin(scope) as transaction:
        assert list(await transaction.stale_connectors([fixture.connector.id])) == []

    await small_chunks(fixture)

    async with store.begin(scope) as transaction:
        [row] = await transaction.stale_connectors([fixture.connector.id])
    assert row.id == fixture.connector.id and row.stale == 1 and row.reprocessing == 0
    assert row.name == fixture.connector.name

    await fixture.reprocessor.start(fixture.actor, fixture.connector.id)
    async with store.begin(scope) as transaction:
        [row] = await transaction.stale_connectors([fixture.connector.id])
    assert row.stale == 0 and row.reprocessing == 1
    await fixture.run_jobs()
    async with store.begin(scope) as transaction:
        assert list(await transaction.stale_connectors([fixture.connector.id])) == []


async def test_the_dashboard_lists_connectors_stale_for_longer_than_a_day(
    fixture: ConnectorFixture,
) -> None:
    await fixture.ingest(("handbook.md", HANDBOOK))
    assert await fixture.reprocessor.alerts(fixture.actor) == []

    await small_chunks(fixture)

    assert await fixture.reprocessor.alerts(fixture.actor) == [], "just marked: not yet a day"
    tomorrow = datetime.now(UTC) + timedelta(hours=25)
    [alert] = await fixture.reprocessor.alerts(fixture.actor, now=tomorrow)
    assert alert.connector_id == fixture.connector.id
    assert alert.connector_name == fixture.connector.name
    assert alert.stale_documents == 1 and 24.9 <= alert.age_hours <= 25.1

    await fixture.reprocessor.start(fixture.actor, fixture.connector.id)
    await fixture.run_jobs()
    assert await fixture.reprocessor.alerts(fixture.actor, now=tomorrow) == []
