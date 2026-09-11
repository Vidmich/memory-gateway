"""The reprocessing store and the connector store's index-status half (task 104), as one
set of assertions run against the memory twins and against PostgreSQL.

What is worth a server here: the one-``UPDATE``-per-format reconciliation with its media
type clause, the grouped index-status counts, the scope selection for a run, the locked
counter increment that closes a run, and the ``regexp_replace`` that moves every row's
model segment at a swap. Each is right by iteration in memory and right in SQL only if
written correctly.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.tenancy import TenantScope
from app.db.models import Connector, Document, Organization
from app.schemas.connector_config import ChunkingConfig
from app.services.connector_store import ConnectorStore, DocumentDraft, ReprocessScope
from app.services.index_fingerprint import index_fingerprint, with_embedding_model
from app.services.reprocessing_store import ReprocessingStore
from tests.reprocessing_support import make_reprocessing_run


@dataclass(frozen=True)
class Fixture:
    connectors: ConnectorStore
    runs: ReprocessingStore
    acme: Organization
    globex: Organization
    acme_connector: Connector
    globex_connector: Connector

    def scope(self, organization: Organization) -> TenantScope:
        return TenantScope(role="org_admin", organization_id=organization.id)

    @property
    def acme_scope(self) -> TenantScope:
        return self.scope(self.acme)

    @property
    def globex_scope(self) -> TenantScope:
        return self.scope(self.globex)


Check = Callable[[Fixture], Awaitable[None]]
CHECKS: list[Check] = []


def check(function: Check) -> Check:
    CHECKS.append(function)
    return function


CURRENT = index_fingerprint(
    ChunkingConfig(),
    embedding_model="hash-bow",
    tokenizer="words",
    context=None,
    extraction_version=1,
)
OLD = index_fingerprint(
    ChunkingConfig(chunk_size=400, overlap=40),
    embedding_model="hash-bow",
    tokenizer="words",
    context=None,
    extraction_version=1,
)
EXPECTED = {"markdown": CURRENT, "text": CURRENT, "pdf": CURRENT, "other": CURRENT}


async def _document(
    fixture: Fixture,
    scope: TenantScope,
    connector: Connector,
    name: str,
    *,
    mime: str | None = "text/markdown",
    status: str = "indexed",
    fingerprint: str | None = OLD,
    index_status: str = "current",
) -> Document:
    async with fixture.connectors.begin(scope) as transaction:
        row = await transaction.connector(connector.id)
        assert row is not None
        document = await transaction.claim_document(
            row,
            DocumentDraft(
                source_uri=f"{connector.storage_prefix}{name}",
                source_name=name,
                size_bytes=2048,
                mime_type=mime,
            ),
            reset=True,
        )
        document.status = status
        document.index_fingerprint = fingerprint
        document.index_status = index_status
        document.chunk_count = 3
        document.embedding_model = "hash-bow"
        await transaction.commit()
        return document


async def _status(fixture: Fixture, scope: TenantScope, document_id: uuid.UUID) -> Document:
    async with fixture.connectors.begin(scope) as transaction:
        found = await transaction.document(document_id)
        assert found is not None
        return found


@check
async def reconciling_marks_stale_rows_by_format_and_leaves_the_rest(fixture: Fixture) -> None:
    """One format at a time, and only rows that are indexed with a readable fingerprint:
    an unrecorded row and one a run owns are left as they are."""
    scope = fixture.acme_scope
    stale_md = await _document(fixture, scope, fixture.acme_connector, "a.md")
    current_md = await _document(
        fixture, scope, fixture.acme_connector, "b.md", fingerprint=CURRENT
    )
    stale_txt = await _document(fixture, scope, fixture.acme_connector, "c.txt", mime="text/plain")
    unrecorded = await _document(fixture, scope, fixture.acme_connector, "d.md", fingerprint=None)
    owned = await _document(
        fixture, scope, fixture.acme_connector, "e.md", index_status="reprocessing"
    )
    pending = await _document(fixture, scope, fixture.acme_connector, "f.md", status="pending")

    async with fixture.connectors.begin(scope) as transaction:
        changed = await transaction.reconcile_index_status(
            fixture.acme_connector.id, EXPECTED, kinds=["markdown"]
        )
        await transaction.commit()

    assert changed == 1
    assert (await _status(fixture, scope, stale_md.id)).index_status == "stale"
    assert (await _status(fixture, scope, current_md.id)).index_status == "current"
    assert (await _status(fixture, scope, stale_txt.id)).index_status == "current"
    assert (await _status(fixture, scope, unrecorded.id)).index_status == "current"
    assert (await _status(fixture, scope, owned.id)).index_status == "reprocessing"
    assert (await _status(fixture, scope, pending.id)).index_status == "current"

    async with fixture.connectors.begin(scope) as transaction:
        everything = await transaction.reconcile_index_status(fixture.acme_connector.id, EXPECTED)
        again = await transaction.reconcile_index_status(fixture.acme_connector.id, EXPECTED)
        await transaction.commit()
    assert everything == 1 and again == 0
    assert (await _status(fixture, scope, stale_txt.id)).index_status == "stale"


@check
async def reconciling_puts_a_row_back_to_current_when_its_fingerprint_matches(
    fixture: Fixture,
) -> None:
    scope = fixture.acme_scope
    row = await _document(
        fixture, scope, fixture.acme_connector, "a.md", fingerprint=CURRENT, index_status="stale"
    )
    async with fixture.connectors.begin(scope) as transaction:
        changed = await transaction.reconcile_index_status(fixture.acme_connector.id, EXPECTED)
        await transaction.commit()
    assert changed == 1
    assert (await _status(fixture, scope, row.id)).index_status == "current"


@check
async def the_other_format_is_the_complement_of_every_known_type(fixture: Fixture) -> None:
    scope = fixture.acme_scope
    odd = await _document(
        fixture, scope, fixture.acme_connector, "blob.bin", mime="application/x-thing"
    )
    untyped = await _document(fixture, scope, fixture.acme_connector, "raw", mime=None)
    markdown = await _document(fixture, scope, fixture.acme_connector, "a.md")

    async with fixture.connectors.begin(scope) as transaction:
        changed = await transaction.reconcile_index_status(
            fixture.acme_connector.id, EXPECTED, kinds=["other"]
        )
        await transaction.commit()

    assert changed == 2
    assert (await _status(fixture, scope, odd.id)).index_status == "stale"
    assert (await _status(fixture, scope, untyped.id)).index_status == "stale"
    assert (await _status(fixture, scope, markdown.id)).index_status == "current"


@check
async def counts_group_by_index_status_and_call_a_blank_fingerprint_unrecorded(
    fixture: Fixture,
) -> None:
    scope = fixture.acme_scope
    await _document(fixture, scope, fixture.acme_connector, "a.md", index_status="stale")
    await _document(fixture, scope, fixture.acme_connector, "b.md", index_status="stale")
    await _document(fixture, scope, fixture.acme_connector, "c.md", index_status="reprocessing")
    await _document(fixture, scope, fixture.acme_connector, "d.md", fingerprint=None)
    await _document(fixture, scope, fixture.acme_connector, "e.md", fingerprint=CURRENT)
    await _document(
        fixture, fixture.globex_scope, fixture.globex_connector, "z.md", index_status="stale"
    )

    async with fixture.connectors.begin(scope) as transaction:
        counts = await transaction.index_status_counts(
            [fixture.acme_connector.id, fixture.globex_connector.id]
        )
        mimes = await transaction.stale_mime_types(fixture.acme_connector.id)
        by_format = await transaction.indexed_by_format(fixture.acme_connector.id)

    acme = counts[fixture.acme_connector.id]
    assert (acme.stale, acme.reprocessing, acme.unrecorded, acme.current) == (2, 1, 1, 1)
    assert fixture.globex_connector.id not in counts, "scoped: Globex's rows are not Acme's"
    assert mimes == {"text/markdown"}
    assert by_format == {"markdown": 5}


@check
async def a_run_takes_the_documents_in_its_scope_and_claims_them(fixture: Fixture) -> None:
    scope = fixture.acme_scope
    stale = await _document(fixture, scope, fixture.acme_connector, "a.md", index_status="stale")
    current = await _document(fixture, scope, fixture.acme_connector, "b.md", fingerprint=CURRENT)
    text = await _document(
        fixture, scope, fixture.acme_connector, "c.txt", mime="text/plain", index_status="stale"
    )
    pending = await _document(fixture, scope, fixture.acme_connector, "d.md", status="pending")
    unrecorded = await _document(fixture, scope, fixture.acme_connector, "e.md", fingerprint=None)
    failed = await _document(fixture, scope, fixture.acme_connector, "f.md", status="failed")
    run_id = uuid.uuid4()

    async with fixture.connectors.begin(scope) as transaction:
        by_stale = await transaction.documents_in_scope(
            fixture.acme_connector.id, ReprocessScope(kind="stale"), after=None, limit=10
        )
        by_format = await transaction.documents_in_scope(
            fixture.acme_connector.id,
            ReprocessScope(kind="formats", formats=frozenset({"text"})),
            after=None,
            limit=10,
        )
        everything = await transaction.documents_in_scope(
            fixture.acme_connector.id, ReprocessScope(kind="all"), after=None, limit=10
        )
        blank = await transaction.documents_in_scope(
            fixture.acme_connector.id, ReprocessScope(kind="unrecorded"), after=None, limit=10
        )
        broken = await transaction.documents_in_scope(
            fixture.acme_connector.id, ReprocessScope(kind="failed"), after=None, limit=10
        )
        paged = await transaction.documents_in_scope(
            fixture.acme_connector.id, ReprocessScope(kind="all"), after=None, limit=2
        )
        rest = await transaction.documents_in_scope(
            fixture.acme_connector.id, ReprocessScope(kind="all"), after=paged[-1].id, limit=10
        )
        assert {row.id for row in by_stale} == {stale.id, text.id}
        assert {row.id for row in by_format} == {text.id}
        assert {row.id for row in everything} == {
            stale.id,
            current.id,
            text.id,
            unrecorded.id,
            failed.id,
        }
        assert pending.id not in {row.id for row in everything}, "a job is already coming"
        assert {row.id for row in blank} == {unrecorded.id}
        assert {row.id for row in broken} == {failed.id}
        assert len(paged) == 2 and {row.id for row in paged} | {row.id for row in rest} == {
            row.id for row in everything
        }

        await transaction.claim_for_run(by_stale, run_id)
        await transaction.commit()

    claimed = await _status(fixture, scope, stale.id)
    assert claimed.status == "pending" and claimed.index_status == "reprocessing"
    assert claimed.reprocessing_run_id == run_id and claimed.chunk_count == 0
    async with fixture.connectors.begin(scope) as transaction:
        owned = await transaction.unfinished_for_run(run_id)
    assert {row.id for row in owned} == {stale.id, text.id}


@check
async def counting_a_finished_document_moves_the_run_and_closes_it_at_the_total(
    fixture: Fixture,
) -> None:
    scope = fixture.acme_scope
    run = make_reprocessing_run(fixture.acme_connector, status="running", total=3, done=0)
    async with fixture.runs.begin(scope) as transaction:
        await transaction.add(run)
        await transaction.commit()

    async with fixture.connectors.begin(scope) as transaction:
        first = await transaction.count_reprocessed(run.id, "done", tokens=10)
        assert first is not None and first.done == 1 and first.finished_at is None
        second = await transaction.count_reprocessed(run.id, "skipped", tokens=0)
        assert second is not None and second.skipped == 1
        third = await transaction.count_reprocessed(run.id, "failed", tokens=0)
        assert third is not None
        await transaction.commit()

    async with fixture.runs.begin(scope) as transaction:
        finished = await transaction.find(run.id)
    assert finished is not None
    assert finished.status == "partial" and finished.finished_at is not None
    assert (finished.done, finished.failed, finished.skipped, finished.spent_tokens) == (
        1,
        1,
        1,
        10,
    )

    async with fixture.connectors.begin(fixture.globex_scope) as transaction:
        assert await transaction.count_reprocessed(run.id, "done", tokens=0) is None
    async with fixture.connectors.begin(scope) as transaction:
        assert await transaction.count_reprocessed(uuid.uuid4(), "done", tokens=0) is None


@check
async def the_runs_read_newest_first_and_one_running_per_connector(fixture: Fixture) -> None:
    scope = fixture.acme_scope
    older = make_reprocessing_run(fixture.acme_connector)
    newer = make_reprocessing_run(fixture.acme_connector, status="running", total=2, done=1)
    newer.started_at = older.started_at.replace(minute=older.started_at.minute) + (
        newer.started_at - older.started_at
    )
    foreign = make_reprocessing_run(fixture.globex_connector, status="running", total=1)
    reindex_run = uuid.uuid4()
    spawned = make_reprocessing_run(
        fixture.acme_connector, trigger="embedding_model", scope="all", reindex_run_id=reindex_run
    )
    async with fixture.runs.begin(scope) as transaction:
        await transaction.add(older)
        await transaction.add(newer)
        await transaction.add(spawned)
        await transaction.commit()
    async with fixture.runs.begin(fixture.globex_scope) as transaction:
        await transaction.add(foreign)
        await transaction.commit()

    async with fixture.runs.begin(scope) as transaction:
        history = await transaction.runs(fixture.acme_connector.id, limit=10)
        running = await transaction.running(fixture.acme_connector.id)
        limited = await transaction.runs(fixture.acme_connector.id, limit=1)
        by_reindex = await transaction.spawned_by(reindex_run)
        open_here = await transaction.unfinished()
        assert await transaction.find(foreign.id) is None, "scoped"
        assert await transaction.running(fixture.globex_connector.id) is None, "scoped"
        connectors = await transaction.connectors()

    assert {row.id for row in history} == {older.id, newer.id, spawned.id}
    assert history[0].started_at >= history[-1].started_at
    assert running is not None and running.id == newer.id
    assert len(limited) == 1
    assert [row.id for row in by_reindex] == [spawned.id]
    assert {row.id for row in open_here} == {newer.id}
    assert {row.id for row in connectors} == {fixture.acme_connector.id}

    platform = TenantScope(role="superadmin", organization_id=None)
    async with fixture.runs.begin(platform) as transaction:
        everywhere = await transaction.unfinished()
        every_connector = await transaction.connectors()
    assert {row.id for row in everywhere} == {newer.id, foreign.id}
    assert {row.id for row in every_connector} == {
        fixture.acme_connector.id,
        fixture.globex_connector.id,
    }


@check
async def adopting_a_model_rewrites_every_indexed_rows_model_segment(fixture: Fixture) -> None:
    scope = fixture.acme_scope
    recorded = await _document(fixture, scope, fixture.acme_connector, "a.md", fingerprint=CURRENT)
    unrecorded = await _document(fixture, scope, fixture.acme_connector, "b.md", fingerprint=None)
    failed = await _document(
        fixture, scope, fixture.acme_connector, "c.md", status="failed", fingerprint=CURRENT
    )
    foreign = await _document(
        fixture, fixture.globex_scope, fixture.globex_connector, "z.md", fingerprint=CURRENT
    )

    async with fixture.connectors.begin(scope) as transaction:
        rewritten = await transaction.rewrite_embedding_model("hash-next")
        await transaction.commit()

    assert rewritten == 1
    moved = await _status(fixture, scope, recorded.id)
    assert moved.embedding_model == "hash-next"
    assert moved.index_fingerprint == with_embedding_model(CURRENT, "hash-next")
    assert (await _status(fixture, scope, unrecorded.id)).index_fingerprint is None
    assert (await _status(fixture, scope, failed.id)).embedding_model == "hash-bow"
    untouched = await _status(fixture, fixture.globex_scope, foreign.id)
    assert untouched.index_fingerprint == CURRENT, "scoped: another tenant's rows stay"


@check
async def the_document_list_filters_on_index_status(fixture: Fixture) -> None:
    scope = fixture.acme_scope
    stale = await _document(fixture, scope, fixture.acme_connector, "a.md", index_status="stale")
    await _document(fixture, scope, fixture.acme_connector, "b.md", fingerprint=CURRENT)

    async with fixture.connectors.begin(scope) as transaction:
        rows = await transaction.documents(
            fixture.acme_connector.id, after=None, limit=10, index_status="stale"
        )
    assert [row.id for row in rows] == [stale.id]


@check
async def stale_summaries_name_each_connector_with_the_oldest_marking(fixture: Fixture) -> None:
    scope = fixture.acme_scope
    await _document(fixture, scope, fixture.acme_connector, "a.md", index_status="stale")
    await _document(fixture, scope, fixture.acme_connector, "b.md", index_status="stale")
    await _document(fixture, scope, fixture.acme_connector, "c.md", fingerprint=CURRENT)
    await _document(
        fixture, fixture.globex_scope, fixture.globex_connector, "z.md", index_status="stale"
    )

    async with fixture.connectors.begin(scope) as transaction:
        summaries = await transaction.stale_summaries()
    platform = TenantScope(role="superadmin", organization_id=None)
    async with fixture.connectors.begin(platform) as transaction:
        everywhere = await transaction.stale_summaries()

    [summary] = summaries
    assert summary.id == fixture.acme_connector.id and summary.stale == 2
    assert summary.name == fixture.acme_connector.name and summary.since is not None
    assert {row.id for row in everywhere} == {
        fixture.acme_connector.id,
        fixture.globex_connector.id,
    }


__all__ = ["CHECKS", "Check", "Fixture", "check"]
