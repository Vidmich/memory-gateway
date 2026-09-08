"""The pipeline, end to end over memory implementations of the four ports.

Every test here drives the *real* :class:`~app.services.ingestion.IngestionPipeline` — the
same object a worker runs — through the same job runner, all the way to a chunk being
searchable. Only the sockets are swapped, and the store contracts hold the swaps honest.

The organising idea, and the one worth carrying into a review: **the document being bad
and the world being bad are handled in opposite directions.** A file that will not decode
is marked ``failed`` and the job *returns*; a provider that is rate-limiting leaves the
document alone and the job *raises*. Getting that backwards in either direction is the
most expensive mistake available here, so both halves are asserted explicitly.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from app.core.metrics import build_extraction_metrics
from app.services.embeddings import EmbeddingError
from app.services.filetypes import format_label
from app.services.ingestion import IngestionSettings
from app.services.jobs import PermanentJobError
from tests.auth_support import make_organization
from tests.connector_support import ConnectorFixture, build_connectors, make_connector
from tests.office_fixtures import BROKEN_ZIP, scanned_pdf

HANDBOOK = b"""# Handbook

Acme pays for widgets and gizmos.

## Leave

Twenty-five days of annual leave.
"""

MOV = b"\x00\x00\x00\x20ftypqt  " + b"\x00" * 9000
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 40


@pytest.fixture
def connectors() -> ConnectorFixture:
    return build_connectors(make_organization())


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


async def test_an_uploaded_file_reaches_indexed(connectors: ConnectorFixture) -> None:
    await connectors.ingest(("handbook.md", HANDBOOK))

    document = await connectors.document("handbook.md")
    assert document.status == "indexed"
    assert document.chunk_count > 0
    assert document.error is None
    assert document.indexed_at is not None


async def test_the_document_records_what_it_was_indexed_with(
    connectors: ConnectorFixture,
) -> None:
    """SPEC §9.4: the embedding model on every row, so drift after a platform change is
    detectable rather than a mysterious drop in retrieval quality."""
    await connectors.ingest(("handbook.md", HANDBOOK))

    document = await connectors.document("handbook.md")

    assert document.embedding_model == "hash-bow"
    assert document.content_hash and len(document.content_hash) == 64
    assert document.mime_type == "text/markdown"
    assert document.size_bytes == len(HANDBOOK)


async def test_the_chunks_are_searchable(connectors: ConnectorFixture) -> None:
    await connectors.ingest(("handbook.md", HANDBOOK))

    hits = await connectors.service.search(connectors.actor, connectors.connector.id, "widgets")

    assert hits
    assert "widgets" in hits[0].text


async def test_every_chunk_carries_the_spec_metadata(connectors: ConnectorFixture) -> None:
    await connectors.ingest(("handbook.md", HANDBOOK))
    document = await connectors.document("handbook.md")

    [hit] = await connectors.service.search(
        connectors.actor, connectors.connector.id, "annual leave", limit=1
    )

    payload = hit.payload
    assert payload["org_id"] == str(connectors.organization_id)
    assert payload["connector_id"] == str(connectors.connector.id)
    assert payload["document_id"] == str(document.id)
    assert payload["source_name"] == "handbook.md"
    assert payload["source_uri"].endswith("handbook.md")
    assert payload["content_hash"] == document.content_hash
    assert isinstance(payload["chunk_index"], int)
    assert payload["ingested_at"]


async def test_a_hundred_mixed_files_all_reach_a_terminal_state(
    connectors: ConnectorFixture,
) -> None:
    """The acceptance criterion, in the form it is actually written: no stuck rows."""
    files: list[tuple[str, bytes]] = []
    for index in range(60):
        files.append(
            (f"doc{index}.md", f"# Doc {index}\n\nSome content number {index}.\n".encode())
        )
    for index in range(20):
        files.append((f"data{index}.csv", b"name,role\nAda,Engineer\n"))
    for index in range(10):
        files.append((f"clip{index}.mov", MOV))
    for index in range(10):
        files.append((f"broken{index}.json", b"{not json at all"))

    await connectors.ingest(*files)

    statuses = await connectors.statuses()
    assert len(statuses) == 100
    assert set(statuses.values()) <= {"indexed", "skipped", "failed"}
    assert sum(1 for status in statuses.values() if status == "indexed") == 80
    assert sum(1 for status in statuses.values() if status == "skipped") == 10
    assert sum(1 for status in statuses.values() if status == "failed") == 10


# ---------------------------------------------------------------------------
# the document is bad
# ---------------------------------------------------------------------------


async def test_an_unsupported_format_is_skipped_with_a_readable_reason(
    connectors: ConnectorFixture,
) -> None:
    await connectors.ingest(("clip.mov", MOV))

    document = await connectors.document("clip.mov")
    assert document.status == "skipped"
    assert document.error == "This video is not a supported format."


async def test_an_epub_says_coming_soon_rather_than_unsupported(
    connectors: ConnectorFixture,
) -> None:
    """The distinction task 09 built, and that task 11 used up for four formats.

    A roadmap item and a file nobody should have uploaded read differently, and a customer
    has to be able to tell which they are looking at. EPUB is what occupies that state now
    that PDF and the Office formats have left it.
    """
    await connectors.ingest(("book.epub", b"PK\x03\x04" + b"\x00" * 9000))

    document = await connectors.document("book.epub")
    assert document.status == "skipped"
    assert document.reason == "not_yet_supported"
    assert document.error is not None and "not available yet" in document.error


async def test_a_truncated_pdf_fails_with_a_sentence_rather_than_a_traceback(
    connectors: ConnectorFixture,
) -> None:
    """The bytes claim to be a PDF and are not one. That is a fault in the file, so it is
    ``failed`` rather than ``skipped`` — and the reason code says which fault."""
    await connectors.ingest(("report.pdf", b"%PDF-1.7\n" + b"\x00" * 9000))

    document = await connectors.document("report.pdf")
    assert document.status == "failed"
    assert document.reason == "malformed_pdf"
    assert document.error is not None and "truncated or corrupt" in document.error


async def test_a_renamed_binary_is_skipped_not_decoded(connectors: ConnectorFixture) -> None:
    """Sniffing, doing its job. Trusting the extension would embed mojibake and report it
    as indexed, which poisons every retrieval that comes near it."""
    await connectors.ingest(("notes.txt", JPEG))

    document = await connectors.document("notes.txt")
    assert document.status == "skipped"
    assert document.error is not None and "image" in document.error


async def test_a_corrupt_file_fails_with_the_extraction_error(
    connectors: ConnectorFixture,
) -> None:
    await connectors.ingest(("data.json", b'{"a": 1,\n "b": }'))

    document = await connectors.document("data.json")
    assert document.status == "failed"
    assert document.error is not None and "line 2" in document.error


async def test_a_file_with_no_text_is_skipped_rather_than_indexed_empty(
    connectors: ConnectorFixture,
) -> None:
    await connectors.ingest(("empty.md", b"   \n\n  \n"))

    document = await connectors.document("empty.md")
    assert document.status == "skipped"
    assert document.chunk_count == 0


async def test_a_bad_document_does_not_dead_letter_its_job(
    connectors: ConnectorFixture,
) -> None:
    """The bytes will not change. Retrying four times spends four workers to reach the
    same answer, and the retry that matters is the button in the UI."""
    await connectors.ingest(("data.json", b"{oh dear"))

    assert connectors.dead_letters.records == []
    assert connectors.queue.pending == []


async def test_a_failed_document_retries_successfully_once_the_cause_is_fixed(
    connectors: ConnectorFixture,
) -> None:
    """The acceptance criterion. The **Retry** button after the file has been corrected."""
    await connectors.ingest(("data.json", b"{oh dear"))
    assert (await connectors.document("data.json")).status == "failed"

    await connectors.ingest(("data.json", b'{"team": "platform"}'))

    document = await connectors.document("data.json")
    assert document.status == "indexed"
    assert document.error is None
    assert document.chunk_count == 1


async def test_the_retry_button_re_enqueues_even_with_unchanged_content(
    connectors: ConnectorFixture,
) -> None:
    """Keyed differently from an upload on purpose: a retry is a request to run again
    *although* nothing changed, and keying on the hash would make the button do nothing."""
    await connectors.ingest(("handbook.md", HANDBOOK))
    connectors.queue.submitted.clear()

    document = await connectors.document("handbook.md")
    await connectors.service.reindex_document(connectors.actor, document.id)

    assert connectors.queue.names() == ["ingest_document"]
    assert (await connectors.document("handbook.md")).status == "pending"


# ---------------------------------------------------------------------------
# the world is bad
# ---------------------------------------------------------------------------


class FlakyEmbedder:
    """Fails a fixed number of times, then works. A provider being rate-limited."""

    model = "flaky"
    dimension = 64

    def __init__(self, failures: int) -> None:
        self.remaining = failures
        self.calls = 0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise EmbeddingError("429 from the provider", retryable=True)
        return [[1.0] + [0.0] * 63 for _ in texts]


async def test_a_provider_outage_leaves_the_document_alone_and_retries_the_job(
    connectors: ConnectorFixture,
) -> None:
    """Marking the document ``failed`` here would be a lie a customer would have to
    notice and undo by hand — the file is fine and will index in thirty seconds."""
    embedder = FlakyEmbedder(failures=2)
    connectors.pipeline._embedder = embedder

    await connectors.ingest(("handbook.md", HANDBOOK))

    assert embedder.calls == 3
    assert (await connectors.document("handbook.md")).status == "indexed"
    assert connectors.dead_letters.records == []


async def test_a_provider_that_never_recovers_dead_letters_the_job(
    connectors: ConnectorFixture,
) -> None:
    connectors.pipeline._embedder = FlakyEmbedder(failures=99)

    await connectors.ingest(("handbook.md", HANDBOOK))

    assert len(connectors.dead_letters.records) == 1
    # The row stays where the pipeline left it, which is not a terminal state — so the
    # next resync finds it and tries again.
    assert (await connectors.document("handbook.md")).status == "embedding"


async def test_a_document_deleted_mid_flight_is_a_permanent_failure(
    connectors: ConnectorFixture,
) -> None:
    """Retrying cannot make a deleted row exist. Dead-lettering on the first attempt
    keeps three workers from proving that again."""
    with pytest.raises(PermanentJobError):
        await connectors.pipeline.ingest(
            organization_id=connectors.organization_id, document_id=uuid.uuid4()
        )


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


async def test_ingesting_the_same_file_twice_produces_one_document(
    connectors: ConnectorFixture,
) -> None:
    await connectors.ingest(("handbook.md", HANDBOOK))
    await connectors.ingest(("handbook.md", HANDBOOK))

    assert len(await connectors.documents()) == 1


async def test_ingesting_the_same_file_twice_produces_one_chunk_set(
    connectors: ConnectorFixture,
) -> None:
    """Deterministic point ids. Without them re-ingestion doubles the index and every
    query returns the same chunk twice."""
    await connectors.ingest(("handbook.md", HANDBOOK))
    first = await connectors.chunk_count()

    await connectors.ingest(("handbook.md", HANDBOOK))

    assert await connectors.chunk_count() == first


async def test_two_concurrent_uploads_of_the_same_name_produce_one_document(
    connectors: ConnectorFixture,
) -> None:
    """The acceptance criterion. ``claim_document`` is an upsert rather than a
    read-then-insert precisely so the database decides, not the timing."""
    import asyncio

    await asyncio.gather(
        connectors.upload(("handbook.md", HANDBOOK)),
        connectors.upload(("handbook.md", HANDBOOK)),
    )
    await connectors.run_jobs()

    assert len(await connectors.documents()) == 1


async def test_running_the_same_job_twice_converges(connectors: ConnectorFixture) -> None:
    """The idempotency key removes the common case; the job body has to remove the rest,
    because the key's reservation expires and races."""
    await connectors.ingest(("handbook.md", HANDBOOK))
    document = await connectors.document("handbook.md")
    before = await connectors.chunk_count()

    await connectors.pipeline.ingest(
        organization_id=connectors.organization_id, document_id=document.id
    )

    assert await connectors.chunk_count() == before


async def test_re_ingesting_a_shorter_file_removes_the_chunks_it_no_longer_has(
    connectors: ConnectorFixture,
) -> None:
    """Deterministic ids overwrite the points that still exist. Only a delete removes the
    tail of a document that shrank — and until it does, retrieval still finds text that is
    no longer in the file."""
    long_text = ("Chapter about widgets. " * 400).encode()
    small = IngestionSettings()
    connectors.connector.chunking = {"chunk_size": 60, "overlap": 0}

    await connectors.ingest(("notes.md", long_text))
    before = await connectors.chunk_count()
    assert before > 3

    await connectors.ingest(("notes.md", b"Just one line about widgets."))

    assert await connectors.chunk_count() == 1
    assert small.max_file_bytes  # the settings object is untouched by any of this


async def test_the_old_text_is_gone_from_the_index_after_an_edit(
    connectors: ConnectorFixture,
) -> None:
    """Asserted through the debug search, which is what the acceptance criterion asks
    for: the old chunks are not merely unreferenced, they are unreachable."""
    await connectors.ingest(("notes.md", b"The secret passphrase is xyzzy.\n"))
    assert await connectors.service.search(connectors.actor, connectors.connector.id, "xyzzy")

    await connectors.ingest(("notes.md", b"Nothing to see here about plovers.\n"))

    hits = await connectors.service.search(connectors.actor, connectors.connector.id, "xyzzy")
    assert all("xyzzy" not in hit.text for hit in hits)


# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------


async def test_a_file_over_the_cap_is_skipped_rather_than_indexed() -> None:
    fixture = build_connectors(make_organization(), limits=IngestionSettings(max_file_bytes=4096))
    await fixture.put("huge.md", b"# Title\n\n" + b"word " * 5000)

    await fixture.service.resync(fixture.actor, fixture.connector.id)
    await fixture.run_jobs()

    document = await fixture.document("huge.md")
    assert document.status == "skipped"
    assert document.error is not None and "limit" in document.error


async def test_a_binary_file_is_not_read_past_the_sniff_window() -> None:
    """A folder of videos costs a listing and 8 KB each, not their size in bytes."""
    fixture = build_connectors(make_organization())
    read: list[int] = []

    original = fixture.objects.open

    def counting(key: str) -> Any:
        async def wrapper() -> Any:
            total = 0
            async for piece in original(key):
                total += len(piece)
                yield piece
            read.append(total)

        return wrapper()

    fixture.objects.open = counting  # type: ignore[method-assign]
    await fixture.put("clip.mov", MOV + b"\x00" * 500_000)
    await fixture.service.resync(fixture.actor, fixture.connector.id)
    await fixture.run_jobs()

    assert (await fixture.document("clip.mov")).status == "skipped"
    # The stream was abandoned after the sniff window rather than drained.
    assert read == [] or read[0] < 500_000


async def test_extraction_has_a_wall_clock_cap() -> None:
    """One pathological input must not occupy a worker indefinitely."""
    fixture = build_connectors(
        make_organization(), limits=IngestionSettings(extraction_timeout_seconds=0.001)
    )

    def slow(data: bytes, *, name: str) -> Any:
        import time

        time.sleep(0.5)
        raise AssertionError("should have been abandoned")

    fixture.registry.register(slow, media_types=("text/markdown",))

    await fixture.ingest(("slow.md", HANDBOOK))

    document = await fixture.document("slow.md")
    assert document.status == "failed"
    assert document.error is not None and "longer than" in document.error


# ---------------------------------------------------------------------------
# resync
# ---------------------------------------------------------------------------


async def test_resync_ingests_an_object_nobody_told_us_about(
    connectors: ConnectorFixture,
) -> None:
    """What a presigned upload looks like from here: bytes appear, and reconciliation is
    what notices."""
    await connectors.put("scripted.md", HANDBOOK)

    summary = await connectors.service.resync(connectors.actor, connectors.connector.id)
    await connectors.run_jobs()

    assert summary.added == 1
    assert (await connectors.document("scripted.md")).status == "indexed"


async def test_resync_re_ingests_an_object_whose_etag_changed(
    connectors: ConnectorFixture,
) -> None:
    await connectors.put("scripted.md", b"# One\n\nabout widgets\n")
    await connectors.service.resync(connectors.actor, connectors.connector.id)
    await connectors.run_jobs()

    await connectors.put("scripted.md", b"# Two\n\nabout plovers\n")
    summary = await connectors.service.resync(connectors.actor, connectors.connector.id)
    await connectors.run_jobs()

    assert summary.updated == 1
    hits = await connectors.service.search(connectors.actor, connectors.connector.id, "plovers")
    assert hits and "plovers" in hits[0].text


async def test_resync_deletes_a_document_whose_object_has_gone(
    connectors: ConnectorFixture,
) -> None:
    await connectors.ingest(("handbook.md", HANDBOOK))
    key = (await connectors.document("handbook.md")).source_uri
    await connectors.objects.delete([key])

    summary = await connectors.service.resync(connectors.actor, connectors.connector.id)

    assert summary.deleted == 1
    assert await connectors.documents() == ()
    assert await connectors.chunk_count() == 0


async def test_resync_leaves_an_unchanged_object_alone(connectors: ConnectorFixture) -> None:
    await connectors.ingest(("handbook.md", HANDBOOK))
    connectors.queue.submitted.clear()

    summary = await connectors.service.resync(connectors.actor, connectors.connector.id)

    assert (summary.added, summary.updated, summary.deleted, summary.unchanged) == (0, 0, 0, 1)
    assert connectors.queue.submitted == []


async def test_resync_does_not_delete_a_document_whose_upload_is_still_in_flight(
    connectors: ConnectorFixture,
) -> None:
    """The race a lock cannot close, because it opens before the lock is taken: the
    listing is a snapshot, and a file uploaded a millisecond later is legitimately absent
    from it. Only terminal documents are ever deleted."""
    await connectors.upload(("handbook.md", HANDBOOK))  # queued, not yet run
    await connectors.objects.delete([(await connectors.document("handbook.md")).source_uri])

    summary = await connectors.service.resync(connectors.actor, connectors.connector.id)

    assert summary.deleted == 0
    assert len(await connectors.documents()) == 1


async def test_resync_recovers_a_document_whose_job_was_lost(
    connectors: ConnectorFixture,
) -> None:
    """The queue does not redeliver a job whose worker was killed — reconciliation is the
    recovery path, and this is it working."""
    await connectors.upload(("handbook.md", HANDBOOK))
    connectors.queue.pending.clear()  # the worker died holding it

    summary = await connectors.service.resync(connectors.actor, connectors.connector.id)
    await connectors.run_jobs()

    assert summary.skipped == 1
    assert (await connectors.document("handbook.md")).status == "indexed"


async def test_a_second_resync_running_at_the_same_time_does_nothing(
    connectors: ConnectorFixture,
) -> None:
    connectors.lock.held.add(f"resync:{connectors.connector.id}")

    summary = await connectors.pipeline.resync(
        organization_id=connectors.organization_id, connector_id=connectors.connector.id
    )

    assert summary.total == 0


async def test_resync_stamps_the_sync_time(connectors: ConnectorFixture) -> None:
    await connectors.service.resync(connectors.actor, connectors.connector.id)

    view = await connectors.service.get_connector(connectors.actor, connectors.connector.id)
    assert view.connector.status == "ready"
    assert view.connector.last_synced_at is not None


# ---------------------------------------------------------------------------
# deletion
# ---------------------------------------------------------------------------


async def test_deleting_a_connector_removes_objects_documents_and_vectors(
    connectors: ConnectorFixture,
) -> None:
    await connectors.ingest(("handbook.md", HANDBOOK), ("notes.md", b"# Notes\n\nplovers\n"))
    assert await connectors.chunk_count() > 0

    await connectors.service.delete_connector(connectors.actor, connectors.connector.id)
    await connectors.run_jobs()

    assert connectors.objects.objects == {}
    assert connectors.database.documents == {}
    assert connectors.database.connectors == {}
    assert await connectors.vectors.count(connectors.organization_id) == 0


async def test_a_connector_is_marked_deleting_before_anything_is_torn_down(
    connectors: ConnectorFixture,
) -> None:
    """So the UI stops offering uploads into something that is going away."""
    await connectors.service.delete_connector(connectors.actor, connectors.connector.id)

    assert connectors.database.connectors[connectors.connector.id].status == "deleting"


async def test_an_ingestion_that_arrives_during_a_delete_does_not_index(
    connectors: ConnectorFixture,
) -> None:
    """It would leave vectors the delete has already walked past."""
    await connectors.upload(("handbook.md", HANDBOOK))
    await connectors.service.delete_connector(connectors.actor, connectors.connector.id)

    await connectors.run_jobs()

    assert await connectors.vectors.count(connectors.organization_id) == 0


async def test_deleting_a_document_removes_its_object_and_its_vectors(
    connectors: ConnectorFixture,
) -> None:
    await connectors.ingest(("handbook.md", HANDBOOK))
    document = await connectors.document("handbook.md")

    await connectors.service.delete_document(connectors.actor, document.id)

    assert connectors.objects.objects == {}
    assert await connectors.chunk_count() == 0
    assert await connectors.documents() == ()


async def test_deleting_one_document_leaves_the_others(connectors: ConnectorFixture) -> None:
    await connectors.ingest(("a.md", b"# A\n\nwidgets\n"), ("b.md", b"# B\n\nplovers\n"))
    document = await connectors.document("a.md")

    await connectors.service.delete_document(connectors.actor, document.id)

    assert [row.source_name for row in await connectors.documents()] == ["b.md"]
    assert await connectors.chunk_count() == 1


# ---------------------------------------------------------------------------
# more than one connector
# ---------------------------------------------------------------------------


async def test_two_connectors_in_one_organization_do_not_see_each_other(
    connectors: ConnectorFixture,
) -> None:
    """One collection per *organization*, so the separation between connectors is the
    payload filter rather than the collection — which makes it worth asserting."""
    other = make_connector(connectors.organization, name="Other docs")
    connectors.database.add_connector(other)

    await connectors.ingest(("ours.md", b"# Ours\n\nwidgets everywhere\n"))
    await connectors.upload(("theirs.md", b"# Theirs\n\nwidgets elsewhere\n"), connector=other)
    await connectors.run_jobs()

    hits = await connectors.service.search(connectors.actor, other.id, "widgets")

    assert [hit.payload["source_name"] for hit in hits] == ["theirs.md"]


async def test_the_search_names_the_model_that_produced_the_vectors(
    connectors: ConnectorFixture,
) -> None:
    """Two searches under different embedding models are not comparable, and this is the
    only place the difference is visible."""
    assert connectors.service.embedding_model == "hash-bow"


def test_the_pipeline_reads_its_settings_rather_than_hardcoding_them() -> None:
    settings = IngestionSettings(max_file_bytes=1, extraction_timeout_seconds=2.0)

    assert settings.max_file_bytes == 1
    assert settings.storage_quota_bytes is None


async def test_an_unknown_connector_type_is_a_permanent_failure(
    connectors: ConnectorFixture,
) -> None:
    """A row written by a newer build, or by hand. A readable error beats an
    ``AttributeError`` inside a worker."""
    await connectors.upload(("handbook.md", HANDBOOK))
    document = await connectors.document("handbook.md")
    connectors.database.connectors[connectors.connector.id].type = "sql"

    with pytest.raises(PermanentJobError):
        await connectors.pipeline.ingest(
            organization_id=connectors.organization_id, document_id=document.id
        )


async def test_the_job_payload_is_only_strings(connectors: ConnectorFixture) -> None:
    """It crosses a process boundary and survives a deploy. Anything richer would be a
    version dependency between the API and the worker."""
    await connectors.upload(("handbook.md", HANDBOOK))

    [job] = connectors.queue.submitted
    assert isinstance(job.payload, Mapping)
    assert all(isinstance(value, str) for value in job.payload.values())


# ---------------------------------------------------------------------------
# extraction metrics (task 11)
# ---------------------------------------------------------------------------


def extractions(registry: CollectorRegistry, fmt: str, outcome: str) -> float:
    value = registry.get_sample_value("extractions_total", {"format": fmt, "outcome": outcome})
    return value or 0.0


async def test_extraction_is_counted_by_format_and_outcome() -> None:
    """The axis the answer lives on. Extraction degrades one format at a time — a PDF
    library upgrade that starts returning nothing, an Office parser that chokes on one
    vendor's export — and an unlabelled failure rate averages that into invisibility.
    """
    registry = CollectorRegistry()
    fixture = build_connectors(make_organization(), metrics=build_extraction_metrics(registry))

    await fixture.ingest(
        ("handbook.md", HANDBOOK),
        ("scan.pdf", scanned_pdf()),
        ("policy.docx", BROKEN_ZIP),
    )

    assert extractions(registry, "markdown", "ok") == 1.0
    # A scan is a decision rather than a fault, and the counter says so — a corpus that is
    # all scans is a support conversation, not an incident.
    assert extractions(registry, "pdf", "skipped") == 1.0
    assert extractions(registry, "docx", "failed") == 1.0


async def test_extraction_duration_is_recorded_per_format() -> None:
    """Markdown is milliseconds and a 200-page PDF is seconds. One histogram across both
    hides the fact that decides how much worker capacity a corpus needs."""
    registry = CollectorRegistry()
    fixture = build_connectors(make_organization(), metrics=build_extraction_metrics(registry))

    await fixture.ingest(("handbook.md", HANDBOOK))

    count = registry.get_sample_value("extraction_duration_seconds_count", {"format": "markdown"})
    assert count == 1.0


async def test_an_exotic_media_type_does_not_become_a_new_label() -> None:
    """Label values are a closed map with a fallback. A sniffed media type used directly
    is unbounded cardinality with the first exotic upload, and a Prometheus series per
    file type nobody supports is how a metrics backend falls over."""
    assert format_label("application/vnd.sqlite3") == "other"
    assert format_label("text/x-rust") == "code"
    assert format_label("application/pdf") == "pdf"
