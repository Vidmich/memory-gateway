"""Document summarization through the pipeline (task 102).

Every test here runs the real :class:`~app.services.ingestion.IngestionPipeline` over the
memory ports, with a scripted model standing in for the provider — so what is asserted is
what a worker does: the points it writes, the row it leaves, the ledger it fills, and what
a failure means in each mode.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from app.api.proxy.errors import UpstreamStatus
from app.core.metrics import build_summarization_metrics
from app.core.tenancy import TenantScope
from app.schemas.connector_config import ChunkingConfig, effective, fingerprint
from app.schemas.summarization import ContextIdentity
from app.services.connectors import ConnectorPatch
from app.services.ingestion import SUMMARIZATION_FAILED
from app.services.jobs import INGEST_DOCUMENT, SUMMARIZE_DOCUMENT
from app.services.summarization import (
    KIND_SOURCE,
    KIND_SUMMARY,
    MANUAL,
    PROMPT_VERSION,
    SUMMARY_INDEX,
    SUMMARY_SECTION,
    contextual_text,
)
from app.services.summarization_store import DAILY_CAP, NO_MODEL, WAITING_ON_CAP
from app.services.summarizer import MALFORMED, PROVIDER_REFUSED
from tests.auth_support import make_organization
from tests.connector_support import ConnectorFixture, ScriptedSummaryModel, build_connectors

SUMMARY = "The travel policy: who may travel, approval thresholds, and how receipts are filed."
HANDBOOK = (
    "# Travel policy\n\n"
    + "\n\n".join(
        f"Section {index}. "
        + " ".join(f"Rule {index}.{j} applies to travel expenses." for j in range(12))
        for index in range(8)
    )
).encode()

SETTINGS = {"chunking": {"chunk_size": 60, "overlap": 0}}


def refusal(status: int) -> UpstreamStatus:
    return UpstreamStatus(status_code=status, model_name="cheap-summarizer", message="no")


async def fixture_with(
    mode: str, *replies: str | Exception, usage: tuple[int, int] | None = (120, 40), **config: Any
) -> ConnectorFixture:
    fixture = build_connectors(
        make_organization(),
        summary_model=ScriptedSummaryModel(*(replies or (SUMMARY,)), usage=usage),
    )
    await fixture.service.update_connector(
        fixture.actor, fixture.connector.id, ConnectorPatch(chunking=SETTINGS["chunking"])
    )
    await fixture.configure_summarization(mode=mode, **config)
    return fixture


def vector_of(fixture: ConnectorFixture, point_id: str) -> list[float]:
    """The stored vector behind a point — the memory store keeps it beside the payload."""
    return list(
        fixture.vectors.collections[fixture.vectors.live(fixture.organization_id)][point_id].vector
    )


def by_kind(points: list[Any]) -> tuple[list[Any], list[Any]]:
    sources = [p for p in points if p.payload.get("kind") == KIND_SOURCE]
    summaries = [p for p in points if p.payload.get("kind") == KIND_SUMMARY]
    return sources, summaries


# ---------------------------------------------------------------------------
# the two modes
# ---------------------------------------------------------------------------


async def test_summary_chunk_adds_exactly_one_labelled_point_and_changes_no_source_chunk() -> None:
    plain = await fixture_with("off")
    await plain.ingest(("handbook.md", HANDBOOK))
    baseline = await plain.points((await plain.document("handbook.md")).id)

    fixture = await fixture_with("summary_chunk")
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")
    points = await fixture.points(document.id)
    sources, summaries = by_kind(points)

    assert len(points) == len(baseline) + 1
    assert [p.payload["text"] for p in sources] == [p.payload["text"] for p in baseline]
    assert all("context" not in p.payload for p in sources)
    assert all(p.payload["kind"] == KIND_SOURCE for p in baseline)
    [summary] = summaries
    assert summary.payload["text"] == SUMMARY
    assert summary.payload["page_or_section"] == SUMMARY_SECTION
    assert summary.payload["chunk_index"] == SUMMARY_INDEX
    assert document.status == "indexed"
    assert document.summary == SUMMARY
    assert document.summary_status == "summarized"
    assert document.summary_model == "cheap-summarizer"
    assert (document.summary_tokens_in, document.summary_tokens_out) == (120, 40)
    assert document.summarized_at is not None
    # The row counts source chunks; the summary is a point, not a chunk of the file.
    assert document.chunk_count == len(sources)


async def test_contextual_keeps_every_text_and_prefixes_every_vector_with_the_summary() -> None:
    plain = await fixture_with("off")
    await plain.ingest(("handbook.md", HANDBOOK))
    baseline = await plain.points((await plain.document("handbook.md")).id)

    fixture = await fixture_with("contextual")
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")
    points = await fixture.points(document.id)
    sources, summaries = by_kind(points)

    assert summaries == []
    assert [p.payload["text"] for p in sources] == [p.payload["text"] for p in baseline]
    assert all(p.payload["context"] == SUMMARY for p in sources)
    assert all(p.payload["embedded_because"] == "context" for p in sources)
    # The vector is of the prefixed text: the same embedder over the same string.
    expected = await fixture.embedder.embed([contextual_text(SUMMARY, sources[0].payload["text"])])
    assert vector_of(fixture, sources[0].id) == pytest.approx(expected[0])
    assert vector_of(fixture, sources[0].id) != vector_of(plain, baseline[0].id)
    # And the fingerprint says the vectors were built with a prefix.
    assert document.chunk_fingerprint != (await plain.document("handbook.md")).chunk_fingerprint


async def test_both_does_both() -> None:
    fixture = await fixture_with("both")
    await fixture.ingest(("handbook.md", HANDBOOK))
    sources, summaries = by_kind(await fixture.points((await fixture.document("handbook.md")).id))

    assert len(summaries) == 1
    assert all(p.payload["context"] == SUMMARY for p in sources)


async def test_a_window_and_a_prefix_are_both_named_on_the_point() -> None:
    fixture = await fixture_with("contextual")
    await fixture.service.update_connector(
        fixture.actor,
        fixture.connector.id,
        ConnectorPatch(chunking={"strategy": "sentence_window", "window_sentences": 1}),
    )
    await fixture.ingest(("handbook.md", HANDBOOK))
    sources, _ = by_kind(await fixture.points((await fixture.document("handbook.md")).id))

    point = sources[1].payload
    assert point["embedded_because"] == "window+context"
    assert point["context"] == SUMMARY
    assert point["embedded_text"] in point["text"]
    assert point["window_sentences"] == 1


async def test_a_format_override_turns_summaries_off_for_the_lockfiles() -> None:
    fixture = await fixture_with("summary_chunk", overrides={"code": {"mode": "off"}})
    await fixture.ingest(
        ("handbook.md", HANDBOOK), ("main.py", b"def travel():\n    return 'ok'\n")
    )

    _, doc_summaries = by_kind(await fixture.points((await fixture.document("handbook.md")).id))
    _, code_summaries = by_kind(await fixture.points((await fixture.document("main.py")).id))
    assert len(doc_summaries) == 1
    assert code_summaries == []
    assert (await fixture.document("main.py")).summary_status is None
    assert fixture.summary_model.calls == 1


# ---------------------------------------------------------------------------
# what a change does to the index
# ---------------------------------------------------------------------------


async def test_switching_summary_chunk_on_reindexes_nothing() -> None:
    fixture = await fixture_with("off")
    await fixture.ingest(("handbook.md", HANDBOOK))

    view = await fixture.service.update_connector(
        fixture.actor, fixture.connector.id, ConnectorPatch(summarization={"mode": "summary_chunk"})
    )

    assert view.reindex_required is False
    assert view.reindex_formats == frozenset()
    listing = await fixture.service.list_documents(fixture.actor, fixture.connector.id)
    assert listing.stale == frozenset()


async def test_switching_contextual_on_marks_every_document_stale_and_says_so() -> None:
    fixture = await fixture_with("off")
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")

    view = await fixture.service.update_connector(
        fixture.actor, fixture.connector.id, ConnectorPatch(summarization={"mode": "contextual"})
    )

    assert view.reindex_required is True
    assert "markdown" in view.reindex_formats
    listing = await fixture.service.list_documents(fixture.actor, fixture.connector.id)
    assert listing.stale == {document.id}

    await fixture.service.reindex_connector(fixture.actor, fixture.connector.id)
    await fixture.run_jobs()
    listing = await fixture.service.list_documents(fixture.actor, fixture.connector.id)
    assert listing.stale == frozenset()
    assert (await fixture.document("handbook.md")).summary == SUMMARY


async def test_the_contextual_fingerprint_carries_the_resolved_model_and_prompt_version() -> None:
    fixture = await fixture_with("contextual")
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")

    expected = fingerprint(
        effective(ChunkingConfig.load(fixture.connector.chunking), "markdown"),
        embedding_model=fixture.embedder.model,
        tokenizer=fixture.pipeline.tokenizer.name,
        context=ContextIdentity(model_id=fixture.summary_row.id, prompt_version=PROMPT_VERSION),
    )
    assert document.chunk_fingerprint == expected


async def test_a_reindex_reuses_a_summary_of_the_same_bytes_instead_of_paying_again() -> None:
    """A chunking change must not buy the corpus a second round of model calls."""
    fixture = await fixture_with("both")
    await fixture.ingest(("handbook.md", HANDBOOK))
    assert fixture.summary_model.calls == 1

    await fixture.service.update_connector(
        fixture.actor, fixture.connector.id, ConnectorPatch(chunking={"chunk_size": 80})
    )
    await fixture.service.reindex_connector(fixture.actor, fixture.connector.id)
    await fixture.run_jobs()

    assert fixture.summary_model.calls == 1
    document = await fixture.document("handbook.md")
    assert document.summary == SUMMARY
    _, summaries = by_kind(await fixture.points(document.id))
    assert len(summaries) == 1
    assert len(fixture.runs()) == 1


async def test_a_changed_file_is_summarized_again() -> None:
    fixture = await fixture_with("summary_chunk", SUMMARY, "A different summary.")
    await fixture.ingest(("handbook.md", HANDBOOK))
    await fixture.ingest(("handbook.md", HANDBOOK + b"\n\nAn appendix about per diems."))

    assert fixture.summary_model.calls == 2
    assert (await fixture.document("handbook.md")).summary == "A different summary."


# ---------------------------------------------------------------------------
# failure, by mode
# ---------------------------------------------------------------------------


async def test_a_refusal_under_summary_chunk_indexes_the_document_and_marks_the_summary() -> None:
    fixture = await fixture_with("summary_chunk", refusal(400))
    await fixture.ingest(("handbook.md", HANDBOOK))

    document = await fixture.document("handbook.md")
    assert document.status == "indexed"
    assert document.summary is None
    assert document.summary_status == "failed"
    assert document.summary_error is not None and "cheap-summarizer" in document.summary_error
    _, summaries = by_kind(await fixture.points(document.id))
    assert summaries == []
    [run] = fixture.runs()
    assert (run.outcome, run.reason) == ("failed", PROVIDER_REFUSED)


async def test_the_summarize_action_retries_just_that_phase() -> None:
    fixture = await fixture_with("summary_chunk", refusal(400), SUMMARY)
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")
    points_before = len(await fixture.points(document.id))

    await fixture.service.regenerate_summary(fixture.actor, document.id)
    assert [job.name for job in fixture.queue.pending] == [SUMMARIZE_DOCUMENT]
    await fixture.run_jobs()

    document = await fixture.document("handbook.md")
    assert document.status == "indexed"
    assert document.summary == SUMMARY
    assert document.summary_status == "summarized"
    assert len(await fixture.points(document.id)) == points_before + 1
    assert fixture.summary_model.calls == 2


async def test_a_refusal_under_contextual_fails_the_document_and_names_the_model() -> None:
    """Half a corpus embedded with context and half without is two corpora that rank
    differently; the degradation rule does not extend to one that changes what every
    other chunk means."""
    fixture = await fixture_with("contextual", refusal(400))
    await fixture.ingest(("handbook.md", HANDBOOK))

    document = await fixture.document("handbook.md")
    assert document.status == "failed"
    assert document.reason == SUMMARIZATION_FAILED
    assert document.error is not None and "cheap-summarizer" in document.error
    assert await fixture.points(document.id) == []
    assert document.summary_status == "failed"


async def test_an_empty_completion_is_a_failure_not_a_retry() -> None:
    fixture = await fixture_with("summary_chunk", "   ")
    await fixture.ingest(("handbook.md", HANDBOOK))

    document = await fixture.document("handbook.md")
    assert document.status == "indexed"
    assert document.summary_status == "failed"
    [run] = fixture.runs()
    assert run.reason == MALFORMED
    assert fixture.summary_model.calls == 1


@pytest.mark.parametrize("mode", ["summary_chunk", "contextual"])
async def test_a_retryable_provider_error_raises_for_the_job_s_backoff(mode: str) -> None:
    """The world is bad, not the document: neither a failed summary nor a failed document,
    and the row it leaves counts toward the cap like the attempt it was."""
    fixture = await fixture_with(mode, refusal(503))
    await fixture.upload(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")

    with pytest.raises(UpstreamStatus):
        await fixture.pipeline.ingest(
            organization_id=fixture.organization_id, document_id=document.id
        )

    document = await fixture.document("handbook.md")
    assert document.status not in ("indexed", "failed")
    assert document.summary_status is None
    [run] = fixture.runs()
    assert run.outcome == "failed"


async def test_a_recovered_provider_indexes_on_the_retry() -> None:
    fixture = await fixture_with("contextual", refusal(503), SUMMARY)
    await fixture.upload(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")
    with pytest.raises(UpstreamStatus):
        await fixture.pipeline.ingest(
            organization_id=fixture.organization_id, document_id=document.id
        )

    await fixture.pipeline.ingest(organization_id=fixture.organization_id, document_id=document.id)

    assert (await fixture.document("handbook.md")).status == "indexed"
    assert [run.outcome for run in fixture.runs()] == ["failed", "succeeded"]


@pytest.mark.parametrize("mode", ["summary_chunk", "contextual"])
async def test_a_model_deleted_from_the_catalog_is_a_failure_that_says_so(mode: str) -> None:
    fixture = await fixture_with(mode)
    del fixture.database.upstream_models[fixture.summary_row.id]
    await fixture.ingest(("handbook.md", HANDBOOK))

    document = await fixture.document("handbook.md")
    if mode == "summary_chunk":
        assert document.status == "indexed"
    else:
        assert document.status == "failed"
        assert document.reason == SUMMARIZATION_FAILED
    assert document.summary_status == "failed"
    assert document.summary_error is not None and "No summarization model" in document.summary_error
    [run] = fixture.runs()
    assert (run.outcome, run.reason) == ("skipped", NO_MODEL)
    assert fixture.summary_model.calls == 0


# ---------------------------------------------------------------------------
# the daily cap
# ---------------------------------------------------------------------------


async def test_the_cap_under_summary_chunk_indexes_without_a_summary_and_queues_tomorrow() -> None:
    fixture = await fixture_with("summary_chunk", daily_document_cap=1)
    await fixture.upload(("one.md", HANDBOOK), ("two.md", HANDBOOK))
    await fixture.run_jobs_until_parked()

    one, two = await fixture.document("one.md"), await fixture.document("two.md")
    assert {one.status, two.status} == {"indexed"}
    assert sorted([one.summary_status, two.summary_status], key=str) == ["capped", "summarized"]
    capped = one if one.summary_status == "capped" else two
    assert capped.summary_error is not None and "midnight" in capped.summary_error
    assert fixture.summary_model.calls == 1

    [job] = fixture.parked
    assert job.name == SUMMARIZE_DOCUMENT
    assert job.payload["document_id"] == str(capped.id)
    tomorrow = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )
    assert 0 < job.delay_seconds <= (tomorrow - datetime.now(UTC)).total_seconds() + 60
    assert job.idempotency_key.endswith(tomorrow.date().isoformat())
    runs = fixture.runs()
    assert [run.outcome for run in runs] == ["succeeded", "skipped"]
    assert runs[1].reason == DAILY_CAP


async def test_the_cap_under_contextual_parks_the_document_rather_than_failing_it() -> None:
    fixture = await fixture_with("contextual", daily_document_cap=1)
    await fixture.upload(("one.md", HANDBOOK), ("two.md", HANDBOOK))
    await fixture.run_jobs_until_parked()

    documents = {d.source_name: d for d in await fixture.documents()}
    parked = next(d for d in documents.values() if d.status == "pending")
    indexed = next(d for d in documents.values() if d.status == "indexed")
    assert parked.reason == WAITING_ON_CAP
    assert parked.error is not None and "midnight" in parked.error
    assert indexed.summary == SUMMARY
    [job] = fixture.parked
    assert job.name == INGEST_DOCUMENT
    assert job.payload["document_id"] == str(parked.id)
    # The connector says how many are waiting.
    health = await fixture.summaries_health()
    assert health.waiting_documents == 1
    assert health.waiting[0].connector_id == fixture.connector.id


async def test_a_refusal_does_not_count_against_the_cap_that_caused_it() -> None:
    fixture = await fixture_with("summary_chunk", daily_document_cap=1)
    await fixture.upload(("one.md", HANDBOOK), ("two.md", HANDBOOK), ("three.md", HANDBOOK))
    await fixture.run_jobs_until_parked()

    async with fixture.summaries.begin(TenantScope.of_organization(fixture.organization_id)) as tx:
        counted = await tx.documents_since(
            fixture.connector.id, datetime.now(UTC) - timedelta(hours=1)
        )
    assert counted == 1


async def test_the_cap_holds_across_two_workers() -> None:
    """Two pipelines over one ledger — the way task 13 asserts its cap — because the guard
    is a count over the table and not a counter in either process."""
    first = await fixture_with("summary_chunk", daily_document_cap=1)
    second = build_connectors(
        first.organization,
        database=first.database,
        objects=first.objects,
        vectors=first.vectors,
        connector=first.connector,
        summary_model=ScriptedSummaryModel(SUMMARY),
    )
    await first.upload(("one.md", HANDBOOK), ("two.md", HANDBOOK))
    one, two = await first.document("one.md"), await first.document("two.md")

    await first.pipeline.ingest(organization_id=first.organization_id, document_id=one.id)
    await second.pipeline.ingest(organization_id=first.organization_id, document_id=two.id)

    assert first.summary_model.calls + second.summary_model.calls == 1
    assert sorted([d.summary_status for d in await first.documents()], key=str) == [
        "capped",
        "summarized",
    ]


async def test_a_per_connector_cap_is_per_connector() -> None:
    fixture = await fixture_with("summary_chunk", daily_document_cap=1)
    from tests.connector_support import make_connector

    other = make_connector(fixture.organization, name="Other")
    other.summarization = {"mode": "summary_chunk", "daily_document_cap": 1}
    fixture.database.add_connector(other)
    await fixture.upload(("one.md", HANDBOOK))
    await fixture.upload(("two.md", HANDBOOK), connector=other)
    await fixture.run_jobs()

    assert fixture.summary_model.calls == 2


# ---------------------------------------------------------------------------
# editing
# ---------------------------------------------------------------------------


async def test_editing_a_summary_re_embeds_the_summary_chunk_and_charges_no_cap() -> None:
    fixture = await fixture_with("summary_chunk", daily_document_cap=1)
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")

    await fixture.service.edit_summary(fixture.actor, document.id, "  The handbook, by hand.  ")
    assert [job.name for job in fixture.queue.pending] == [SUMMARIZE_DOCUMENT]
    await fixture.run_jobs()

    document = await fixture.document("handbook.md")
    assert document.summary == "The handbook, by hand."
    assert document.summary_model == MANUAL
    assert document.summary_tokens_in is None
    _, [summary] = by_kind(await fixture.points(document.id))
    assert summary.payload["text"] == "The handbook, by hand."
    expected = await fixture.embedder.embed(["The handbook, by hand."])
    assert vector_of(fixture, summary.id) == pytest.approx(expected[0])
    # One model call, one ledger row: the edit charged nothing, even at a cap of one.
    assert fixture.summary_model.calls == 1
    assert len(fixture.runs()) == 1


async def test_editing_a_summary_under_contextual_re_embeds_the_document_with_it() -> None:
    fixture = await fixture_with("contextual")
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")

    await fixture.service.edit_summary(fixture.actor, document.id, "By hand.")
    assert [job.name for job in fixture.queue.pending] == [INGEST_DOCUMENT]
    await fixture.run_jobs()

    document = await fixture.document("handbook.md")
    assert document.status == "indexed"
    assert document.summary_model == MANUAL
    sources, _ = by_kind(await fixture.points(document.id))
    assert all(p.payload["context"] == "By hand." for p in sources)
    assert fixture.summary_model.calls == 1


async def test_a_manual_summary_survives_a_reindex_and_a_regeneration_replaces_it() -> None:
    fixture = await fixture_with("summary_chunk", SUMMARY, "Regenerated.")
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")
    await fixture.service.edit_summary(fixture.actor, document.id, "By hand.")
    await fixture.run_jobs()

    await fixture.service.reindex_document(fixture.actor, document.id)
    await fixture.run_jobs()
    assert (await fixture.document("handbook.md")).summary == "By hand."
    assert fixture.summary_model.calls == 1

    await fixture.service.regenerate_summary(fixture.actor, document.id)
    await fixture.run_jobs()
    regenerated = await fixture.document("handbook.md")
    assert regenerated.summary == "Regenerated."
    assert regenerated.summary_model == "cheap-summarizer"
    assert fixture.summary_model.calls == 2


async def test_a_summary_cannot_be_written_where_nothing_would_embed_it() -> None:
    from app.core.errors import Validation

    fixture = await fixture_with("off")
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")

    with pytest.raises(Validation):
        await fixture.service.edit_summary(fixture.actor, document.id, "x")
    with pytest.raises(Validation):
        await fixture.service.regenerate_summary(fixture.actor, document.id)


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------


async def test_every_attempt_is_a_row_with_the_provider_s_tokens() -> None:
    registry = CollectorRegistry()
    metrics = build_summarization_metrics(registry)
    fixture = build_connectors(
        make_organization(),
        summary_model=ScriptedSummaryModel(SUMMARY, usage=(321, 45)),
        summarization_metrics=metrics,
    )
    await fixture.configure_summarization(mode="summary_chunk")
    await fixture.ingest(("handbook.md", HANDBOOK))

    [run] = fixture.runs()
    assert run.outcome == "succeeded"
    assert (run.tokens_in, run.tokens_out, run.estimated) == (321, 45, False)
    assert run.model_name == "cheap-summarizer"
    assert run.model_id == fixture.summary_row.id
    assert run.connector_id == fixture.connector.id
    assert registry.get_sample_value("summarization_runs_total", {"outcome": "succeeded"}) == 1
    assert (
        registry.get_sample_value(
            "summarization_tokens_total", {"direction": "in", "model": "cheap-summarizer"}
        )
        == 321
    )
    assert (
        registry.get_sample_value(
            "summarization_tokens_total", {"direction": "out", "model": "cheap-summarizer"}
        )
        == 45
    )
    assert registry.get_sample_value("summarization_duration_seconds_count") == 1


async def test_a_provider_that_reports_no_usage_gets_an_estimate_that_is_flagged() -> None:
    fixture = await fixture_with("summary_chunk", SUMMARY, usage=None)
    await fixture.ingest(("handbook.md", HANDBOOK))

    [run] = fixture.runs()
    assert run.estimated is True
    assert run.tokens_in > 50
    # The word tokenizer's count of the summary, punctuation and all — an estimate, flagged.
    assert run.tokens_out == len(fixture.pipeline.tokenizer.offsets(SUMMARY)) - 1
    document = await fixture.document("handbook.md")
    assert document.summary_tokens_in == run.tokens_in


async def test_the_panel_s_daily_total_is_the_sum_of_the_rows() -> None:
    fixture = await fixture_with("summary_chunk", SUMMARY, refusal(400), SUMMARY)
    await fixture.ingest(("one.md", HANDBOOK), ("two.md", HANDBOOK), ("three.md", HANDBOOK))

    health = await fixture.summaries_health()

    rows = fixture.runs()
    assert health.runs == 3
    assert health.documents == 2
    assert health.failures == 1
    assert health.failure_rate == pytest.approx(1 / 3)
    assert health.tokens_in == sum(run.tokens_in for run in rows)
    assert health.tokens_out == sum(run.tokens_out for run in rows)
    [day] = health.days
    assert (day.documents, day.failures, day.tokens_in) == (2, 1, health.tokens_in)
    [model] = health.by_model
    assert (model.model_name, model.runs) == ("cheap-summarizer", 3)
    [top] = health.top_connectors
    assert (top.connector_id, top.name, top.documents) == (fixture.connector.id, "Product docs", 2)


async def test_the_panel_can_be_narrowed_to_one_connector() -> None:
    fixture = await fixture_with("summary_chunk")
    await fixture.ingest(("one.md", HANDBOOK))

    assert (await fixture.summaries_health(connector_id=fixture.connector.id)).runs == 1
    assert (await fixture.summaries_health(connector_id=uuid.uuid4())).runs == 0


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


async def test_compare_shows_the_prefix_ingestion_used_and_prices_the_call() -> None:
    fixture = await fixture_with("contextual")
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")
    sources, _ = by_kind(await fixture.points(document.id))

    result = await fixture.service.preview_chunking(
        fixture.actor,
        fixture.connector.id,
        document.id,
        candidates=[{"label": "bigger", "chunk_size": 120}],
    )

    assert result.summarization is not None
    assert result.summarization.prefixes is True
    assert result.summarization.summary == SUMMARY
    assert result.summarization.tokens_out == 150
    assert 0 < result.summarization.tokens_in <= 12_000
    current = result.candidates[0]
    # Parity: the previewed prefix is byte-for-byte the ingested one.
    assert [chunk.context for chunk in current.chunks] == [p.payload["context"] for p in sources]
    assert current.chunks[0].text == sources[0].payload["text"]


async def test_compare_prices_the_call_but_shows_no_prefix_under_summary_chunk() -> None:
    fixture = await fixture_with("summary_chunk")
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")

    result = await fixture.service.preview_chunking(
        fixture.actor, fixture.connector.id, document.id
    )

    assert result.summarization is not None
    assert result.summarization.prefixes is False
    assert all(chunk.context is None for chunk in result.candidates[0].chunks)


async def test_compare_says_nothing_about_summaries_when_they_are_off() -> None:
    fixture = await fixture_with("off")
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")

    result = await fixture.service.preview_chunking(
        fixture.actor, fixture.connector.id, document.id
    )

    assert result.summarization is None


# ---------------------------------------------------------------------------
# retrieval and the prompt
# ---------------------------------------------------------------------------


async def test_a_question_about_the_corpus_finds_the_summary_and_the_prompt_labels_it() -> None:
    from app.schemas.gateway_config import MemoryConfig
    from app.schemas.openai import ChatMessage
    from app.services.prompt import render_documents

    fixture = await fixture_with(
        "summary_chunk", "Travel policy summary: approvals thresholds receipts."
    )
    await fixture.ingest(("handbook.md", HANDBOOK))
    document = await fixture.document("handbook.md")

    retrieval = await fixture.memory._retriever.documents(
        organization_id=fixture.organization_id,
        config=MemoryConfig(connector_ids=[fixture.connector.id], doc_top_k=3, doc_min_score=0.0),
        messages=[
            ChatMessage(role="user", content="travel policy summary approvals thresholds receipts")
        ],
    )

    summaries = [chunk for chunk in retrieval.chunks if chunk.is_summary]
    assert len(summaries) == 1
    assert summaries[0].document_id == str(document.id)
    assert summaries[0].page_or_section is None
    block = render_documents(retrieval.chunks)
    assert "summary of: handbook.md" in block
    assert block.count("source: handbook.md") == len(retrieval.chunks) - 1


# ---------------------------------------------------------------------------
# the model chain
# ---------------------------------------------------------------------------


async def test_the_model_chain_prefers_the_connector_then_the_organization_then_the_platform() -> (
    None
):
    from tests.connector_support import make_summary_model

    fixture = await fixture_with("summary_chunk")
    organization = fixture.database.add_organization(fixture.organization)
    own = make_summary_model("org-summarizer")
    own.organization_id = fixture.organization_id
    own.scope = "org"
    fixture.database.add_model(own)
    connector_choice = make_summary_model("connector-summarizer")
    fixture.database.add_model(connector_choice)
    models = fixture.summary_models

    async def resolved(model_id: uuid.UUID | None) -> uuid.UUID | None:
        choice = await models.describe(fixture.organization_id, model_id)
        return choice.id if choice else None

    assert await resolved(None) == fixture.summary_row.id
    organization.settings = {"distillation": {"model_id": str(own.id)}}
    assert await resolved(None) == own.id
    organization.settings = {
        "distillation": {"model_id": str(own.id)},
        "summarization": {"model_id": str(fixture.summary_row.id)},
    }
    assert await resolved(None) == fixture.summary_row.id
    assert await resolved(connector_choice.id) == connector_choice.id
    explained = await models.explain(fixture.organization_id)
    assert explained is not None and explained.source == "summarization"
    organization.settings = {}
    explained = await models.explain(fixture.organization_id)
    assert explained is not None and explained.source == "platform"


async def test_the_connector_view_names_the_model_it_would_use_and_whether_it_inherited_it() -> (
    None
):
    from app.schemas.connector import ConnectorResponse

    fixture = await fixture_with("summary_chunk")
    view = await fixture.service.get_connector(fixture.actor, fixture.connector.id)
    response = ConnectorResponse.of(view)

    assert response.summary_model is not None
    assert response.summary_model.name == "cheap-summarizer"
    assert response.summary_model.inherited is True
    assert response.effective_summarization["markdown"].mode == "summary_chunk"

    await fixture.configure_summarization(model_id=str(fixture.summary_row.id))
    response = ConnectorResponse.of(
        await fixture.service.get_connector(fixture.actor, fixture.connector.id)
    )
    assert response.summary_model is not None and response.summary_model.inherited is False
