"""The control-plane rules: naming, keys, quotas, chunking changes, and the debug search.

The tests worth reading twice are the ones about the **object key**. Everything that keeps
one tenant's files out of another's rests on one string, and a customer supplies part of
it. So :func:`safe_key` gets a table of hostile filenames, and the create path gets a test
that the prefix is derived rather than accepted.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.errors import Conflict, NotFound, Validation
from app.services.connectors import ConnectorDraft, ConnectorPatch, safe_key
from app.services.ingestion import IngestionSettings, ResyncSummary
from tests.auth_support import make_organization
from tests.connector_support import ConnectorFixture, MemoryUpload, build_connectors

HANDBOOK = b"# Handbook\n\nAcme pays for widgets.\n"


@pytest.fixture
def connectors() -> ConnectorFixture:
    return build_connectors(make_organization())


# ---------------------------------------------------------------------------
# object keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("handbook.md", "handbook.md"),
        ("docs/api/auth.md", "docs/api/auth.md"),
        ("docs\\api\\auth.md", "docs/api/auth.md"),
        ("../../../etc/passwd", "etc/passwd"),
        ("/absolute/path.md", "absolute/path.md"),
        ("./relative.md", "relative.md"),
        ("a/../../b.md", "a/b.md"),
        ("  spaced.md  ", "spaced.md"),
        ("weird\x00name.md", "weirdname.md"),
    ],
)
def test_a_filename_cannot_escape_the_connector_prefix(supplied: str, expected: str) -> None:
    """Path separators survive — dragging in a folder should keep its structure, and
    ``docs/api/auth.md`` is more useful in a citation than ``auth.md``. Everything that
    could climb out of the prefix does not."""
    assert safe_key(supplied) == expected


@pytest.mark.parametrize("supplied", ["", "   ", "..", "../..", "/", "///"])
def test_a_filename_that_reduces_to_nothing_is_refused(supplied: str) -> None:
    with pytest.raises(Validation):
        safe_key(supplied)


def test_an_absurdly_long_filename_is_refused() -> None:
    with pytest.raises(Validation, match="too long"):
        safe_key("a" * 2000 + ".md")


async def test_the_storage_prefix_is_derived_from_the_ids(connectors: ConnectorFixture) -> None:
    """The one string every isolation guarantee for objects rests on. A connector that
    could name its own prefix could name another tenant's."""
    view = await connectors.service.create_connector(
        connectors.actor, ConnectorDraft(name="Handbook")
    )

    assert view.connector.storage_prefix == (
        f"orgs/{connectors.organization_id}/connectors/{view.connector.id}/"
    )


async def test_an_upload_lands_under_the_connectors_prefix(
    connectors: ConnectorFixture,
) -> None:
    await connectors.upload(("docs/api/auth.md", HANDBOOK))

    [key] = list(connectors.objects.objects)
    assert key.startswith(connectors.connector.storage_prefix or "")
    assert key.endswith("docs/api/auth.md")


# ---------------------------------------------------------------------------
# creating and editing
# ---------------------------------------------------------------------------


async def test_a_new_connector_starts_ready_and_empty(connectors: ConnectorFixture) -> None:
    view = await connectors.service.create_connector(
        connectors.actor, ConnectorDraft(name="Handbook")
    )

    assert view.connector.status == "ready"
    assert view.document_count == 0
    assert view.total_bytes == 0


async def test_a_new_connector_gets_the_spec_chunking_defaults(
    connectors: ConnectorFixture,
) -> None:
    """Stored as ``{}`` and answered in full, so what is on screen is what will happen."""
    from app.schemas.connector_config import ChunkingConfig

    view = await connectors.service.create_connector(
        connectors.actor, ConnectorDraft(name="Handbook")
    )

    config = ChunkingConfig.load(view.connector.chunking)
    assert (config.strategy, config.chunk_size, config.overlap) == ("recursive", 1000, 150)


async def test_a_duplicate_name_is_refused(connectors: ConnectorFixture) -> None:
    await connectors.service.create_connector(connectors.actor, ConnectorDraft(name="Handbook"))

    with pytest.raises(Conflict):
        await connectors.service.create_connector(connectors.actor, ConnectorDraft(name="Handbook"))


async def test_a_blank_name_is_refused(connectors: ConnectorFixture) -> None:
    with pytest.raises(Validation, match="needs a name"):
        await connectors.service.create_connector(connectors.actor, ConnectorDraft(name="   "))


async def test_an_unsupported_connector_type_is_refused(connectors: ConnectorFixture) -> None:
    with pytest.raises(Validation, match="managed_file_drop"):
        await connectors.service.create_connector(
            connectors.actor, ConnectorDraft(name="Warehouse", type="sql")
        )


async def test_an_unknown_chunking_key_is_refused(connectors: ConnectorFixture) -> None:
    """A stored setting nothing reads is indistinguishable from a setting that does not
    work, and the second is what the user will conclude."""
    with pytest.raises(Validation, match="chunk_sizee"):
        await connectors.service.create_connector(
            connectors.actor, ConnectorDraft(name="Handbook", chunking={"chunk_sizee": 500})
        )


async def test_a_chunking_change_is_partial(connectors: ConnectorFixture) -> None:
    view = await connectors.service.update_connector(
        connectors.actor, connectors.connector.id, ConnectorPatch(chunking={"chunk_size": 500})
    )

    assert view.connector.chunking["chunk_size"] == 500
    assert view.connector.chunking["overlap"] == 150


async def test_changing_the_chunking_says_a_reindex_is_needed(
    connectors: ConnectorFixture,
) -> None:
    """Said at the moment the change is made, rather than in a banner that is always on
    and therefore always ignored."""
    await connectors.ingest(("handbook.md", HANDBOOK))

    view = await connectors.service.update_connector(
        connectors.actor, connectors.connector.id, ConnectorPatch(chunking={"chunk_size": 500})
    )

    assert view.reindex_required is True


async def test_renaming_a_connector_does_not_ask_for_a_reindex(
    connectors: ConnectorFixture,
) -> None:
    await connectors.ingest(("handbook.md", HANDBOOK))

    view = await connectors.service.update_connector(
        connectors.actor, connectors.connector.id, ConnectorPatch(name="Renamed")
    )

    assert view.reindex_required is False


async def test_an_empty_connector_is_not_told_to_reindex(connectors: ConnectorFixture) -> None:
    """Telling somebody to reindex nothing is noise they will learn to ignore."""
    view = await connectors.service.update_connector(
        connectors.actor, connectors.connector.id, ConnectorPatch(chunking={"chunk_size": 500})
    )

    assert view.reindex_required is False


async def test_a_missing_connector_is_a_404(connectors: ConnectorFixture) -> None:
    with pytest.raises(NotFound):
        await connectors.service.get_connector(connectors.actor, uuid.uuid4())


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------


async def test_every_file_gets_its_own_outcome(connectors: ConnectorFixture) -> None:
    """Dropping forty files in and being told "413" because one was a video is not an
    answer anybody can act on."""
    outcomes = await connectors.service.upload(
        connectors.actor,
        connectors.connector.id,
        [MemoryUpload("a.md", HANDBOOK), MemoryUpload("b.md", HANDBOOK)],
    )

    assert [outcome.status for outcome in outcomes] == ["pending", "pending"]
    assert all(outcome.document_id is not None for outcome in outcomes)


async def test_one_oversized_file_does_not_reject_the_others() -> None:
    fixture = build_connectors(make_organization(), limits=IngestionSettings(max_file_bytes=1024))

    outcomes = await fixture.service.upload(
        fixture.actor,
        fixture.connector.id,
        [
            MemoryUpload("small.md", HANDBOOK),
            MemoryUpload("huge.md", b"x" * 5000),
            MemoryUpload("also-small.md", HANDBOOK),
        ],
    )

    assert [outcome.status for outcome in outcomes] == ["pending", "rejected", "pending"]
    assert outcomes[1].error is not None and "larger than" in outcomes[1].error
    assert outcomes[1].document_id is None


async def test_a_rejected_file_leaves_no_object_and_no_document() -> None:
    fixture = build_connectors(make_organization(), limits=IngestionSettings(max_file_bytes=1024))

    await fixture.service.upload(
        fixture.actor, fixture.connector.id, [MemoryUpload("huge.md", b"x" * 5000)]
    )

    assert fixture.objects.objects == {}
    assert await fixture.documents() == ()


async def test_a_file_with_no_name_is_rejected_not_stored(connectors: ConnectorFixture) -> None:
    outcomes = await connectors.service.upload(
        connectors.actor, connectors.connector.id, [MemoryUpload("", HANDBOOK)]
    )

    assert outcomes[0].status == "rejected"
    assert connectors.objects.objects == {}


async def test_an_upload_enqueues_ingestion_after_the_row_exists(
    connectors: ConnectorFixture,
) -> None:
    outcomes = await connectors.service.upload(
        connectors.actor, connectors.connector.id, [MemoryUpload("a.md", HANDBOOK)]
    )

    [job] = connectors.queue.submitted
    assert job.payload["document_id"] == str(outcomes[0].document_id)
    assert await connectors.service.list_documents(connectors.actor, connectors.connector.id)


async def test_the_content_type_is_sniffed_not_taken_from_the_name(
    connectors: ConnectorFixture,
) -> None:
    await connectors.upload(("notes.txt", b"\xff\xd8\xff\xe0" + bytes(range(256)) * 40))

    document = await connectors.document("notes.txt")
    assert document.mime_type == "image/jpeg"


async def test_uploading_into_a_connector_being_deleted_is_refused(
    connectors: ConnectorFixture,
) -> None:
    await connectors.service.delete_connector(connectors.actor, connectors.connector.id)

    with pytest.raises(Conflict, match="being deleted"):
        await connectors.upload(("a.md", HANDBOOK))


async def test_a_storage_quota_stops_the_upload_before_the_bytes_are_stored() -> None:
    fixture = build_connectors(
        make_organization(), limits=IngestionSettings(storage_quota_bytes=2048)
    )
    await fixture.service.upload(
        fixture.actor, fixture.connector.id, [MemoryUpload("first.md", b"x" * 2000)]
    )

    outcomes = await fixture.service.upload(
        fixture.actor, fixture.connector.id, [MemoryUpload("second.md", b"x" * 2000)]
    )

    assert outcomes[0].status == "rejected"
    assert "storage" in (outcomes[0].error or "")
    assert list(fixture.objects.objects) == [f"{fixture.connector.storage_prefix}first.md"]


async def test_the_quota_counts_what_is_already_stored() -> None:
    """Read from the document rows rather than by listing every object under every
    prefix, which would be O(objects) network calls on the request path."""
    fixture = build_connectors(
        make_organization(), limits=IngestionSettings(storage_quota_bytes=10_000)
    )

    await fixture.service.upload(
        fixture.actor,
        fixture.connector.id,
        [MemoryUpload(f"f{index}.md", b"x" * 3000) for index in range(4)],
    )

    outcomes = await fixture.service.upload(
        fixture.actor, fixture.connector.id, [MemoryUpload("last.md", b"x" * 3000)]
    )
    assert outcomes[0].status == "rejected"


# ---------------------------------------------------------------------------
# presigned uploads
# ---------------------------------------------------------------------------


async def test_a_presigned_url_names_a_key_under_the_prefix(
    connectors: ConnectorFixture,
) -> None:
    presigned = await connectors.service.presigned_upload(
        connectors.actor, connectors.connector.id, "scripted.md"
    )

    assert presigned.key == f"{connectors.connector.storage_prefix}scripted.md"
    assert presigned.expires_in == 15 * 60


async def test_a_presigned_url_cannot_be_pointed_outside_the_prefix(
    connectors: ConnectorFixture,
) -> None:
    """A signature for another tenant's prefix would be a write no later check could
    undo, which makes this the sharpest version of the key rule."""
    presigned = await connectors.service.presigned_upload(
        connectors.actor, connectors.connector.id, "../../../other-org/secret.md"
    )

    assert presigned.key.startswith(connectors.connector.storage_prefix or "")


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------


async def test_the_list_carries_counts_by_status(connectors: ConnectorFixture) -> None:
    await connectors.ingest(
        ("good.md", HANDBOOK),
        ("clip.mov", b"\x00\x00\x00\x20ftypqt  " + b"\x00" * 9000),
        ("bad.json", b"{oh dear"),
    )

    page = await connectors.service.list_connectors(connectors.actor)

    [view] = [item for item in page.items if item.connector.id == connectors.connector.id]
    assert view.counts == {"indexed": 1, "skipped": 1, "failed": 1}
    assert view.document_count == 3
    assert view.total_bytes > 0


async def test_documents_can_be_filtered_by_status(connectors: ConnectorFixture) -> None:
    await connectors.ingest(("good.md", HANDBOOK), ("bad.json", b"{oh dear"))

    page = await connectors.service.list_documents(
        connectors.actor, connectors.connector.id, status="failed"
    )

    assert [document.source_name for document in page.items] == ["bad.json"]


async def test_the_document_list_pages(connectors: ConnectorFixture) -> None:
    await connectors.upload(*[(f"f{index}.md", HANDBOOK) for index in range(5)])

    first = await connectors.service.list_documents(
        connectors.actor, connectors.connector.id, limit=2
    )

    assert len(first.items) == 2
    assert first.next_cursor is not None

    second = await connectors.service.list_documents(
        connectors.actor, connectors.connector.id, limit=2, cursor=first.next_cursor
    )
    assert {row.id for row in first.items}.isdisjoint({row.id for row in second.items})


async def test_a_bad_cursor_is_a_422_rather_than_page_one(connectors: ConnectorFixture) -> None:
    """Silently returning page one when the client asked for page four is the kind of
    failure that gets diagnosed as data loss."""
    with pytest.raises(Validation):
        await connectors.service.list_documents(
            connectors.actor, connectors.connector.id, cursor="not-a-uuid"
        )


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


async def test_the_debug_search_finds_an_indexed_chunk(connectors: ConnectorFixture) -> None:
    await connectors.ingest(("handbook.md", HANDBOOK))

    hits = await connectors.service.search(connectors.actor, connectors.connector.id, "widgets")

    assert hits and hits[0].score > 0


async def test_an_empty_query_is_refused(connectors: ConnectorFixture) -> None:
    with pytest.raises(Validation, match="search for"):
        await connectors.service.search(connectors.actor, connectors.connector.id, "   ")


async def test_the_search_limit_is_clamped(connectors: ConnectorFixture) -> None:
    """A caller asking for ten thousand chunks would page an entire tenant's index into
    one response."""
    await connectors.ingest(*[(f"f{index}.md", HANDBOOK) for index in range(3)])

    hits = await connectors.service.search(
        connectors.actor, connectors.connector.id, "widgets", limit=10_000
    )

    assert len(hits) <= 50


async def test_searching_a_connector_with_nothing_indexed_returns_nothing(
    connectors: ConnectorFixture,
) -> None:
    assert await connectors.service.search(connectors.actor, connectors.connector.id, "x") == []


async def test_a_resync_that_fails_says_so_on_the_connector(
    connectors: ConnectorFixture,
) -> None:
    """Otherwise the row reads `syncing` forever with nothing anywhere saying why — and
    the Resync button somebody presses again is the only feedback they get."""

    async def broken(**_: object) -> ResyncSummary:
        raise RuntimeError("storage refused the listing")

    connectors.pipeline.resync = broken  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await connectors.service.resync(connectors.actor, connectors.connector.id)

    view = await connectors.service.get_connector(connectors.actor, connectors.connector.id)
    assert view.connector.status == "error"
    assert view.connector.error == "storage refused the listing"


async def test_a_successful_resync_clears_a_previous_failure(
    connectors: ConnectorFixture,
) -> None:
    connectors.connector.status = "error"
    connectors.connector.error = "storage refused the listing"

    await connectors.service.resync(connectors.actor, connectors.connector.id)

    view = await connectors.service.get_connector(connectors.actor, connectors.connector.id)
    assert view.connector.status == "ready"
    assert view.connector.error is None


async def test_an_upload_stops_when_the_connector_is_deleted_mid_batch(
    connectors: ConnectorFixture,
) -> None:
    """One short transaction per file rather than one across the batch, so this is
    checkable at all — and worth checking, because the alternative writes rows into a
    connector whose objects are already being removed."""

    class Slow(MemoryUpload):
        """Marks the connector `deleting` the moment its own bytes are read."""

        def __init__(self, name: str, data: bytes, fixture: ConnectorFixture) -> None:
            super().__init__(name, data)
            self._fixture = fixture

        async def read(self, size: int = -1) -> bytes:
            self._fixture.database.connectors[self._fixture.connector.id].status = "deleting"
            return await super().read(size)

    outcomes = await connectors.service.upload(
        connectors.actor,
        connectors.connector.id,
        [MemoryUpload("first.md", HANDBOOK), Slow("second.md", HANDBOOK, connectors)],
    )

    assert outcomes[0].status == "pending"
    assert outcomes[1].status == "rejected"
    assert "being deleted" in (outcomes[1].error or "")
    assert [row.source_name for row in await connectors.documents()] == ["first.md"]
