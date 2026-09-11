"""Persistence for connectors and documents, behind a port.

Same shape and same reasons as :mod:`app.services.gateway_store`: what is worth testing —
resync reconciles four cases correctly, ingesting the same object twice makes one row, a
connector's documents are invisible to another tenant — is a rule about state, and those
tests should run on every commit without a database container.

One method here is unlike anything in the earlier stores, and it is the important one.
:meth:`ConnectorTransaction.claim_document` is an **upsert**, not a read-then-insert. Two
uploads of the same filename racing each other, or an upload racing the resync that
listed it, both reach this method at the same instant; a ``SELECT`` followed by an
``INSERT`` produces either a duplicate row or an integrity error depending on the timing,
and the acceptance criterion says one document. ``ON CONFLICT (connector_id, source_uri)
DO UPDATE`` makes the database decide, which is the only participant that can.

The memory implementation does the same thing with a dictionary keyed on the same pair,
so a test that passes there is testing the same rule — not a weaker one that happens to
hold because nothing was concurrent.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import case, false, func, or_, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import uuid7
from app.core.tenancy import TenantScope
from app.db.models import Connector, Document, ReprocessingRun
from app.db.models.connector import TERMINAL_DOCUMENT_STATUSES
from app.db.models.reprocessing import settle
from app.db.repositories import ConnectorRepository, DocumentIndexRow, DocumentRepository
from app.db.scoping import scoped
from app.services.audit import (
    AuditingTransaction,
    MemoryAuditRecorder,
    PostgresAuditRecorder,
)
from app.services.filetypes import KNOWN_MEDIA_TYPES, format_label, media_types_of
from app.services.memory_db import MemoryDatabase


@dataclass(frozen=True, slots=True)
class DocumentAuditRow:
    """What an index audit (task 103) needs from a document row: a name, a format, a size,
    and what it was cut and embedded with. Six columns rather than rows, for the reason
    :meth:`ConnectorTransaction.index` gives."""

    id: uuid.UUID
    source_name: str
    mime_type: str | None
    size_bytes: int
    status: str
    #: Task 104: the structured fingerprint. The audit reads this one; ``chunk_fingerprint``
    #: is gone from the row's readers.
    index_fingerprint: str | None
    embedding_model: str | None


@dataclass(frozen=True, slots=True)
class StaleCounts:
    """A connector's documents by index status (task 104), for the badge and the header."""

    current: int = 0
    stale: int = 0
    reprocessing: int = 0
    #: Indexed rows with no readable fingerprint: shown as *unrecorded*, never as stale.
    unrecorded: int = 0


@dataclass(frozen=True, slots=True)
class StaleSummary:
    """One connector with stale documents, and since when — the oldest marking among
    them, which is the row's ``updated_at`` at the moment the save marked it (task 104).
    What the dashboard's degraded list is built from."""

    id: uuid.UUID
    name: str
    stale: int
    since: datetime


@dataclass(frozen=True, slots=True)
class ReprocessScope:
    """Which of a connector's documents a reprocessing run takes (task 104).

    ``stale`` is the default and the reason the run exists: the old endpoint could only
    reprocess everything, and re-ingesting a thousand current documents to recut twelve
    stale ones is a bill nobody asked for. ``formats`` narrows to kinds; ``all`` is every
    terminal document; ``unrecorded`` the rows with no fingerprint to compare; ``failed``
    the ones a previous run left failed — **Retry failed**.
    """

    kind: str = "stale"
    formats: frozenset[str] = frozenset()
    #: For ``failed``: only documents the named run left failed.
    run_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class DocumentDraft:
    """What is known about an object before anything has read it."""

    source_uri: str
    source_name: str
    size_bytes: int = 0
    etag: str | None = None
    mime_type: str | None = None


class ConnectorTransaction(AuditingTransaction, Protocol):
    """One unit of work, already scoped. Returned objects are live in both
    implementations: mutate one and commit."""

    @property
    def scope(self) -> TenantScope: ...

    async def connector(self, connector_id: uuid.UUID) -> Connector | None: ...

    async def connectors(self, *, after: uuid.UUID | None, limit: int) -> Sequence[Connector]: ...

    async def name_taken(self, name: str, *, excluding: uuid.UUID | None = None) -> bool: ...

    async def add_connector(self, connector: Connector) -> Connector: ...

    async def delete_connector(self, connector: Connector) -> None: ...

    async def document(self, document_id: uuid.UUID) -> Document | None: ...

    async def documents(
        self,
        connector_id: uuid.UUID,
        *,
        after: uuid.UUID | None,
        limit: int,
        status: str | None = None,
        index_status: str | None = None,
    ) -> Sequence[Document]: ...

    async def stale_mime_types(self, connector_id: uuid.UUID) -> set[str | None]:
        """The media types of the connector's stale documents, for the formats the
        header names (task 104)."""
        ...

    async def indexed_by_format(self, connector_id: uuid.UUID) -> Mapping[str, int]:
        """Indexed documents per format kind — what a save is about to mark stale."""
        ...

    async def document_by_source(
        self, connector_id: uuid.UUID, source_uri: str
    ) -> Document | None: ...

    async def claim_document(
        self, connector: Connector, draft: DocumentDraft, *, reset: bool
    ) -> Document:
        """Insert the document, or take over the existing row for this object.

        ``reset`` marks the row ``pending`` again with its counters cleared — what an
        upload or a changed ETag means. Without it the existing row is returned untouched,
        which is what a resync wants for an object it has decided not to re-ingest.
        """
        ...

    async def delete_document(self, document: Document) -> None: ...

    async def document_counts(
        self, connector_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, Mapping[str, int]]: ...

    async def document_bytes(
        self, connector_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, int]: ...

    async def organization_bytes(self) -> int:
        """Total stored bytes for the organization, for the quota check."""
        ...

    async def index(self, connector_id: uuid.UUID) -> Sequence[DocumentIndexRow]:
        """``(id, source_uri, etag, status)`` for every document. The resync input."""
        ...

    async def audit_rows(self, connector_id: uuid.UUID) -> Sequence[DocumentAuditRow]:
        """Every document of a connector, as an audit sees it (task 103)."""
        ...

    # -- index status (task 104) -------------------------------------------

    async def reconcile_index_status(
        self,
        connector_id: uuid.UUID,
        expected: Mapping[str, str],
        *,
        kinds: Sequence[str] | None = None,
    ) -> int:
        """Set every indexed document's ``index_status`` from its fingerprint.

        ``expected`` is the effective fingerprint per format kind; ``kinds`` narrows the
        pass to the formats a change touched, and ``None`` covers them all (the nightly
        reconciliation). One ``UPDATE`` per kind, not a comparison per row at read time:
        a row whose fingerprint equals the expected one becomes ``current``, any other
        recorded one ``stale``. Rows a run owns (``reprocessing``) and rows with no
        fingerprint are left alone. Returns how many rows changed, which for the nightly
        pass is the number of rows the stored status had wrong.
        """
        ...

    async def index_status_counts(
        self, connector_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, StaleCounts]: ...

    async def stale_summaries(self) -> Sequence[StaleSummary]:
        """Every connector in the scope with stale documents, oldest marking first."""
        ...

    async def documents_in_scope(
        self,
        connector_id: uuid.UUID,
        scope: ReprocessScope,
        *,
        after: uuid.UUID | None,
        limit: int,
    ) -> Sequence[Document]:
        """A page of the documents a reprocessing run would take, oldest first, terminal
        rows only — a document with a job already coming is not reset under it."""
        ...

    async def claim_for_run(self, documents: Sequence[Document], run_id: uuid.UUID) -> None:
        """Reset these rows to ``pending`` under the run: ``index_status`` becomes
        ``reprocessing`` and the run id is written, so a run a worker abandoned can be
        continued over exactly the rows it still owns."""
        ...

    async def unfinished_for_run(self, run_id: uuid.UUID) -> Sequence[Document]:
        """The rows a run still owns that have not reached a terminal state — what
        continuing it re-enqueues."""
        ...

    async def count_reprocessed(
        self, run_id: uuid.UUID, outcome: str, *, tokens: int
    ) -> ReprocessingRun | None:
        """One document of the run finished as ``done``, ``failed`` or ``skipped``. Bumps
        the counter and the spend atomically, and closes the run when every document has
        settled. Returns the run, or ``None`` when no such run exists."""
        ...

    async def rewrite_embedding_model(self, embedding_model: str) -> int:
        """After a platform reindex swapped the collection (task 17): every indexed row in
        the scope now holds vectors from ``embedding_model``. Rewrites the row's
        ``embedding_model`` and the fingerprint's embedding segment, leaving the other
        segments — the chunks did not change — and leaving unrecorded rows unrecorded."""
        ...

    async def commit(self) -> None: ...


class ConnectorStore(Protocol):
    def begin(self, scope: TenantScope) -> AbstractAsyncContextManager[ConnectorTransaction]:
        """Open a scoped transaction: ``async with store.begin(scope) as tx``."""
        ...


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


class PostgresConnectorTransaction(PostgresAuditRecorder):
    def __init__(self, session: AsyncSession, scope: TenantScope) -> None:
        self._session = session
        self._scope = scope
        self._connectors = ConnectorRepository(session, scope)
        self._documents = DocumentRepository(session, scope)

    @property
    def scope(self) -> TenantScope:
        return self._scope

    async def connector(self, connector_id: uuid.UUID) -> Connector | None:
        return await self._connectors.get(connector_id)

    async def connectors(self, *, after: uuid.UUID | None, limit: int) -> Sequence[Connector]:
        return await self._connectors.fetch(self._connectors.page(after=after, limit=limit))

    async def name_taken(self, name: str, *, excluding: uuid.UUID | None = None) -> bool:
        return await self._connectors.name_taken(name, excluding=excluding)

    async def add_connector(self, connector: Connector) -> Connector:
        return await self._connectors.add(connector)

    async def delete_connector(self, connector: Connector) -> None:
        await self._connectors.delete(connector)

    async def document(self, document_id: uuid.UUID) -> Document | None:
        return await self._documents.get(document_id)

    async def documents(
        self,
        connector_id: uuid.UUID,
        *,
        after: uuid.UUID | None,
        limit: int,
        status: str | None = None,
        index_status: str | None = None,
    ) -> Sequence[Document]:
        statement = self._documents.page(connector_id, after=after, limit=limit, status=status)
        if index_status is not None:
            statement = statement.where(Document.index_status == index_status)
        return await self._documents.fetch(statement)

    async def stale_mime_types(self, connector_id: uuid.UUID) -> set[str | None]:
        statement = (
            select(Document.mime_type)
            .where(
                self._scope.clause(Document),
                Document.connector_id == connector_id,
                Document.index_status == "stale",
            )
            .distinct()
            .execution_options(**scoped())
        )
        return {row[0] for row in (await self._session.execute(statement)).all()}

    async def indexed_by_format(self, connector_id: uuid.UUID) -> Mapping[str, int]:
        statement = (
            select(Document.mime_type, func.count())
            .where(
                self._scope.clause(Document),
                Document.connector_id == connector_id,
                Document.status == "indexed",
            )
            .group_by(Document.mime_type)
            .execution_options(**scoped())
        )
        counts: dict[str, int] = {}
        for mime, count in (await self._session.execute(statement)).all():
            kind = format_label(mime or "")
            counts[kind] = counts.get(kind, 0) + int(count)
        return counts

    async def document_by_source(self, connector_id: uuid.UUID, source_uri: str) -> Document | None:
        return await self._documents.by_source(connector_id, source_uri)

    async def claim_document(
        self, connector: Connector, draft: DocumentDraft, *, reset: bool
    ) -> Document:
        values = {
            "id": uuid7(),
            "organization_id": connector.organization_id,
            "connector_id": connector.id,
            "source_uri": draft.source_uri,
            "source_name": draft.source_name,
            "size_bytes": draft.size_bytes,
            "etag": draft.etag,
            "mime_type": draft.mime_type,
            "status": "pending",
        }
        statement = pg_insert(Document).values(**values)
        if reset:
            statement = statement.on_conflict_do_update(
                constraint="uq_documents_connector_id_source_uri",
                set_={
                    "source_name": draft.source_name,
                    "size_bytes": draft.size_bytes,
                    "etag": draft.etag,
                    "mime_type": draft.mime_type,
                    "status": "pending",
                    # Cleared together with the status. A row that says `pending` and
                    # still shows the previous run's error and chunk count is a screen
                    # nobody can read.
                    "error": None,
                    "chunk_count": 0,
                    "indexed_at": None,
                    # A new version on its way: nothing is claimed about the old points
                    # until ingestion writes the row again (task 104).
                    "index_status": "current",
                    "reprocessing_run_id": None,
                    "updated_at": datetime.now(UTC),
                },
            )
        else:
            statement = statement.on_conflict_do_nothing(
                constraint="uq_documents_connector_id_source_uri"
            )
        await self._session.execute(statement.execution_options(**scoped()))
        # Read back rather than using RETURNING: `DO NOTHING` returns no row, and the
        # caller needs the existing document in exactly that case.
        found = await self._documents.by_source(connector.id, draft.source_uri)
        if found is None:  # pragma: no cover - the insert above guarantees a row
            raise RuntimeError(f"document row vanished for {draft.source_uri}")
        return found

    async def delete_document(self, document: Document) -> None:
        await self._documents.delete(document)

    async def document_counts(
        self, connector_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, Mapping[str, int]]:
        return await self._documents.status_counts(connector_ids)

    async def document_bytes(self, connector_ids: Sequence[uuid.UUID]) -> Mapping[uuid.UUID, int]:
        return await self._documents.total_bytes(connector_ids)

    async def organization_bytes(self) -> int:
        return await self._documents.organization_bytes()

    async def index(self, connector_id: uuid.UUID) -> Sequence[DocumentIndexRow]:
        return await self._documents.index(connector_id)

    async def audit_rows(self, connector_id: uuid.UUID) -> Sequence[DocumentAuditRow]:
        statement = (
            select(
                Document.id,
                Document.source_name,
                Document.mime_type,
                Document.size_bytes,
                Document.status,
                Document.index_fingerprint,
                Document.embedding_model,
            )
            .where(self._scope.clause(Document), Document.connector_id == connector_id)
            .order_by(Document.id)
            .execution_options(**scoped())
        )
        rows = (await self._session.execute(statement)).all()
        return [DocumentAuditRow(*row) for row in rows]

    # -- index status (task 104) -------------------------------------------

    async def reconcile_index_status(
        self,
        connector_id: uuid.UUID,
        expected: Mapping[str, str],
        *,
        kinds: Sequence[str] | None = None,
    ) -> int:
        changed = 0
        for kind in kinds if kinds is not None else list(expected):
            fingerprint = expected.get(kind)
            if fingerprint is None:
                continue
            wanted = case((Document.index_fingerprint == fingerprint, "current"), else_="stale")
            statement = (
                update(Document)
                .where(
                    self._scope.clause(Document),
                    Document.connector_id == connector_id,
                    Document.status == "indexed",
                    Document.index_fingerprint.is_not(None),
                    Document.index_status != "reprocessing",
                    Document.index_status != wanted,
                    _format_clause(kind),
                )
                .values(index_status=wanted)
                .execution_options(**scoped())
            )
            result = await self._session.execute(statement)
            changed += int(getattr(result, "rowcount", 0) or 0)
        return changed

    async def index_status_counts(
        self, connector_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, StaleCounts]:
        if not connector_ids:
            return {}
        unrecorded = case(
            (
                (Document.status == "indexed") & Document.index_fingerprint.is_(None),
                "unrecorded",
            ),
            else_=Document.index_status,
        )
        statement = (
            select(Document.connector_id, unrecorded, func.count())
            .where(self._scope.clause(Document), Document.connector_id.in_(list(connector_ids)))
            .group_by(Document.connector_id, unrecorded)
            .execution_options(**scoped())
        )
        buckets: dict[uuid.UUID, dict[str, int]] = {}
        for connector_id, status, count in (await self._session.execute(statement)).all():
            buckets.setdefault(connector_id, {})[str(status)] = int(count)
        return {identifier: StaleCounts(**counts) for identifier, counts in buckets.items()}

    async def stale_summaries(self) -> Sequence[StaleSummary]:
        statement = (
            select(
                Connector.id,
                Connector.name,
                func.count(),
                func.min(Document.updated_at),
            )
            .join(Document, Document.connector_id == Connector.id)
            .where(self._scope.clause(Connector), Document.index_status == "stale")
            .group_by(Connector.id, Connector.name)
            .order_by(func.min(Document.updated_at), Connector.name)
            .execution_options(**scoped())
        )
        return [
            StaleSummary(id=identifier, name=name, stale=int(count), since=since)
            for identifier, name, count, since in (await self._session.execute(statement)).all()
        ]

    async def documents_in_scope(
        self,
        connector_id: uuid.UUID,
        scope: ReprocessScope,
        *,
        after: uuid.UUID | None,
        limit: int,
    ) -> Sequence[Document]:
        statement = (
            select(Document)
            .where(
                self._scope.clause(Document),
                Document.connector_id == connector_id,
                Document.status.in_(list(TERMINAL_DOCUMENT_STATUSES)),
                _scope_clause(scope),
            )
            .order_by(Document.id)
            .limit(limit)
            .execution_options(**scoped())
        )
        if after is not None:
            statement = statement.where(Document.id > after)
        return list((await self._session.execute(statement)).scalars().all())

    async def claim_for_run(self, documents: Sequence[Document], run_id: uuid.UUID) -> None:
        for document in documents:
            _claim(document, run_id)
        await self._session.flush()

    async def unfinished_for_run(self, run_id: uuid.UUID) -> Sequence[Document]:
        statement = (
            select(Document)
            .where(
                self._scope.clause(Document),
                Document.reprocessing_run_id == run_id,
                Document.status.not_in(list(TERMINAL_DOCUMENT_STATUSES)),
            )
            .order_by(Document.id)
            .execution_options(**scoped())
        )
        return list((await self._session.execute(statement)).scalars().all())

    async def count_reprocessed(
        self, run_id: uuid.UUID, outcome: str, *, tokens: int
    ) -> ReprocessingRun | None:
        # Locked for the update, so two documents finishing at once increment rather
        # than overwrite — the counters are the progress bar, and a lost increment is a
        # run that never reaches its total.
        statement = (
            select(ReprocessingRun)
            .where(self._scope.clause(ReprocessingRun), ReprocessingRun.id == run_id)
            .with_for_update()
            .execution_options(**scoped())
        )
        run = (await self._session.execute(statement)).scalar_one_or_none()
        if run is None:
            return None
        settle(run, outcome, tokens=tokens)
        await self._session.flush()
        return run

    async def rewrite_embedding_model(self, embedding_model: str) -> int:
        from app.services.index_fingerprint import embedding_segment

        statement = (
            update(Document)
            .where(
                self._scope.clause(Document),
                Document.status == "indexed",
                Document.index_fingerprint.is_not(None),
            )
            .values(
                embedding_model=embedding_model,
                index_fingerprint=func.regexp_replace(
                    Document.index_fingerprint, "em=[0-9a-f]+", embedding_segment(embedding_model)
                ),
            )
            .execution_options(**scoped())
        )
        result = await self._session.execute(statement)
        return int(getattr(result, "rowcount", 0) or 0)

    async def commit(self) -> None:
        await self._session.commit()


def _format_clause(kind: str) -> Any:
    """``mime_type`` expressed as a format kind, for the one place a format has to be
    named in SQL. ``other`` is the complement of every known type, and a row that never
    had its type sniffed is ``other`` too."""
    if kind == "other":
        return or_(
            Document.mime_type.is_(None), Document.mime_type.not_in(sorted(KNOWN_MEDIA_TYPES))
        )
    return Document.mime_type.in_(sorted(media_types_of(kind)))


def _scope_clause(scope: ReprocessScope) -> Any:
    if scope.kind == "stale":
        return Document.index_status == "stale"
    if scope.kind == "formats":
        return (
            or_(*(_format_clause(kind) for kind in sorted(scope.formats)))
            if scope.formats
            else false()
        )
    if scope.kind == "unrecorded":
        return (Document.status == "indexed") & Document.index_fingerprint.is_(None)
    if scope.kind == "failed":
        # Not the sources that are gone: a recut needs the bytes, and retrying a document
        # whose object was deleted would fail it again for the same reason.
        clause = (Document.status == "failed") & (
            or_(Document.reason.is_(None), Document.reason != "missing_object")
        )
        if scope.run_id is not None:
            clause = clause & (Document.reprocessing_run_id == scope.run_id)
        return clause
    return true()


def _in_scope(document: Document, scope: ReprocessScope) -> bool:
    """The memory twin of :func:`_scope_clause`, over one row."""
    if scope.kind == "stale":
        return document.index_status == "stale"
    if scope.kind == "formats":
        return format_label(document.mime_type or "") in scope.formats
    if scope.kind == "unrecorded":
        return document.status == "indexed" and document.index_fingerprint is None
    if scope.kind == "failed":
        return (
            document.status == "failed"
            and document.reason != "missing_object"
            and (scope.run_id is None or document.reprocessing_run_id == scope.run_id)
        )
    return True


def _claim(document: Document, run_id: uuid.UUID) -> None:
    document.status = "pending"
    document.error = None
    document.reason = None
    document.chunk_count = 0
    document.indexed_at = None
    document.index_status = "reprocessing"
    document.reprocessing_run_id = run_id


class PostgresConnectorStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[ConnectorTransaction]:
        async with self._session_factory() as session:
            yield PostgresConnectorTransaction(session, scope)


# ---------------------------------------------------------------------------
# in memory
# ---------------------------------------------------------------------------


class MemoryConnectorTransaction(MemoryAuditRecorder):
    def __init__(self, database: MemoryDatabase, scope: TenantScope) -> None:
        self._db = database
        self._scope = scope

    @property
    def scope(self) -> TenantScope:
        return self._scope

    async def connector(self, connector_id: uuid.UUID) -> Connector | None:
        found = self._db.connectors.get(connector_id)
        if found is None or not self._scope.permits(found.organization_id):
            return None
        return found

    async def connectors(self, *, after: uuid.UUID | None, limit: int) -> Sequence[Connector]:
        rows = [
            connector
            for connector in self._db.connectors.values()
            if self._scope.permits(connector.organization_id)
        ]
        ordered = sorted(rows, key=lambda row: row.id, reverse=True)
        if after is not None:
            ordered = [row for row in ordered if row.id < after]
        return ordered[: limit + 1]

    async def name_taken(self, name: str, *, excluding: uuid.UUID | None = None) -> bool:
        wanted = name.strip()
        return any(
            connector.name == wanted
            and connector.id != excluding
            and self._scope.permits(connector.organization_id)
            for connector in self._db.connectors.values()
        )

    async def add_connector(self, connector: Connector) -> Connector:
        connector.organization_id = self._scope.require_organization()
        return self._db.add_connector(connector)

    async def delete_connector(self, connector: Connector) -> None:
        self._db.connectors.pop(connector.id, None)
        # `ON DELETE CASCADE` in the schema; the same here, so a test cannot pass against
        # orphaned documents the database would have removed.
        for document in list(self._db.documents.values()):
            if document.connector_id == connector.id:
                self._db.documents.pop(document.id, None)

    async def document(self, document_id: uuid.UUID) -> Document | None:
        found = self._db.documents.get(document_id)
        if found is None or not self._scope.permits(found.organization_id):
            return None
        return found

    async def documents(
        self,
        connector_id: uuid.UUID,
        *,
        after: uuid.UUID | None,
        limit: int,
        status: str | None = None,
        index_status: str | None = None,
    ) -> Sequence[Document]:
        rows = [
            document
            for document in self._db.documents.values()
            if document.connector_id == connector_id
            and self._scope.permits(document.organization_id)
            and (status is None or document.status == status)
            and (index_status is None or document.index_status == index_status)
        ]
        ordered = sorted(rows, key=lambda row: row.id, reverse=True)
        if after is not None:
            ordered = [row for row in ordered if row.id < after]
        return ordered[: limit + 1]

    async def stale_mime_types(self, connector_id: uuid.UUID) -> set[str | None]:
        return {row.mime_type for row in self._rows(connector_id) if row.index_status == "stale"}

    async def indexed_by_format(self, connector_id: uuid.UUID) -> Mapping[str, int]:
        counts: dict[str, int] = {}
        for row in self._rows(connector_id):
            if row.status == "indexed":
                kind = format_label(row.mime_type or "")
                counts[kind] = counts.get(kind, 0) + 1
        return counts

    async def document_by_source(self, connector_id: uuid.UUID, source_uri: str) -> Document | None:
        for document in self._db.documents.values():
            if (
                document.connector_id == connector_id
                and document.source_uri == source_uri
                and self._scope.permits(document.organization_id)
            ):
                return document
        return None

    async def claim_document(
        self, connector: Connector, draft: DocumentDraft, *, reset: bool
    ) -> Document:
        existing = await self.document_by_source(connector.id, draft.source_uri)
        if existing is not None:
            if reset:
                existing.source_name = draft.source_name
                existing.size_bytes = draft.size_bytes
                existing.etag = draft.etag
                existing.mime_type = draft.mime_type
                existing.status = "pending"
                existing.error = None
                existing.chunk_count = 0
                existing.indexed_at = None
                existing.index_status = "current"
                existing.reprocessing_run_id = None
            return existing
        return self._db.add_document(
            Document(
                id=uuid7(),
                organization_id=connector.organization_id,
                connector_id=connector.id,
                source_uri=draft.source_uri,
                source_name=draft.source_name,
                size_bytes=draft.size_bytes,
                etag=draft.etag,
                mime_type=draft.mime_type,
                status="pending",
                chunk_count=0,
                index_status="current",
            )
        )

    async def delete_document(self, document: Document) -> None:
        self._db.documents.pop(document.id, None)

    async def document_counts(
        self, connector_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, Mapping[str, int]]:
        wanted = set(connector_ids)
        counts: dict[uuid.UUID, dict[str, int]] = {}
        for document in self._db.documents.values():
            if document.connector_id in wanted and self._scope.permits(document.organization_id):
                bucket = counts.setdefault(document.connector_id, {})
                bucket[document.status] = bucket.get(document.status, 0) + 1
        return counts

    async def document_bytes(self, connector_ids: Sequence[uuid.UUID]) -> Mapping[uuid.UUID, int]:
        wanted = set(connector_ids)
        totals: dict[uuid.UUID, int] = {}
        for document in self._db.documents.values():
            if document.connector_id in wanted and self._scope.permits(document.organization_id):
                totals[document.connector_id] = (
                    totals.get(document.connector_id, 0) + document.size_bytes
                )
        return totals

    async def organization_bytes(self) -> int:
        return sum(
            document.size_bytes
            for document in self._db.documents.values()
            if self._scope.permits(document.organization_id)
        )

    async def index(self, connector_id: uuid.UUID) -> Sequence[DocumentIndexRow]:
        return [
            (document.id, document.source_uri, document.etag, document.status)
            for document in self._db.documents.values()
            if document.connector_id == connector_id
            and self._scope.permits(document.organization_id)
        ]

    async def audit_rows(self, connector_id: uuid.UUID) -> Sequence[DocumentAuditRow]:
        return [
            DocumentAuditRow(
                id=document.id,
                source_name=document.source_name,
                mime_type=document.mime_type,
                size_bytes=document.size_bytes,
                status=document.status,
                index_fingerprint=document.index_fingerprint,
                embedding_model=document.embedding_model,
            )
            for document in sorted(self._db.documents.values(), key=lambda row: row.id)
            if document.connector_id == connector_id
            and self._scope.permits(document.organization_id)
        ]

    # -- index status (task 104) -------------------------------------------

    def _rows(self, connector_id: uuid.UUID) -> list[Document]:
        return sorted(
            (
                document
                for document in self._db.documents.values()
                if document.connector_id == connector_id
                and self._scope.permits(document.organization_id)
            ),
            key=lambda row: row.id,
        )

    async def reconcile_index_status(
        self,
        connector_id: uuid.UUID,
        expected: Mapping[str, str],
        *,
        kinds: Sequence[str] | None = None,
    ) -> int:
        wanted = set(kinds) if kinds is not None else set(expected)
        changed = 0
        for document in self._rows(connector_id):
            kind = format_label(document.mime_type or "")
            if (
                kind not in wanted
                or kind not in expected
                or document.status != "indexed"
                or document.index_fingerprint is None
                or document.index_status == "reprocessing"
            ):
                continue
            status = "current" if document.index_fingerprint == expected[kind] else "stale"
            if document.index_status != status:
                document.index_status = status
                # What the ORM's `onupdate` does for the SQL twin: the marking is dated.
                document.updated_at = datetime.now(UTC)
                changed += 1
        return changed

    async def index_status_counts(
        self, connector_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, StaleCounts]:
        counts: dict[uuid.UUID, dict[str, int]] = {}
        for connector_id in connector_ids:
            for document in self._rows(connector_id):
                bucket = counts.setdefault(connector_id, {})
                key = (
                    "unrecorded"
                    if document.status == "indexed" and document.index_fingerprint is None
                    else (document.index_status or "current")
                )
                bucket[key] = bucket.get(key, 0) + 1
        return {identifier: StaleCounts(**bucket) for identifier, bucket in counts.items()}

    async def stale_summaries(self) -> Sequence[StaleSummary]:
        found: list[StaleSummary] = []
        for connector in self._db.connectors.values():
            if not self._scope.permits(connector.organization_id):
                continue
            stale = [
                row
                for row in self._rows(connector.id)
                if row.index_status == "stale" and row.updated_at is not None
            ]
            if stale:
                found.append(
                    StaleSummary(
                        id=connector.id,
                        name=connector.name,
                        stale=len(stale),
                        since=min(row.updated_at for row in stale),
                    )
                )
        found.sort(key=lambda row: (row.since, row.name))
        return found

    async def documents_in_scope(
        self,
        connector_id: uuid.UUID,
        scope: ReprocessScope,
        *,
        after: uuid.UUID | None,
        limit: int,
    ) -> Sequence[Document]:
        rows = [
            document
            for document in self._rows(connector_id)
            if document.status in TERMINAL_DOCUMENT_STATUSES
            and (after is None or document.id > after)
            and _in_scope(document, scope)
        ]
        return rows[:limit]

    async def claim_for_run(self, documents: Sequence[Document], run_id: uuid.UUID) -> None:
        for document in documents:
            _claim(document, run_id)

    async def unfinished_for_run(self, run_id: uuid.UUID) -> Sequence[Document]:
        return [
            document
            for document in sorted(self._db.documents.values(), key=lambda row: row.id)
            if document.reprocessing_run_id == run_id
            and document.status not in TERMINAL_DOCUMENT_STATUSES
            and self._scope.permits(document.organization_id)
        ]

    async def count_reprocessed(
        self, run_id: uuid.UUID, outcome: str, *, tokens: int
    ) -> ReprocessingRun | None:
        run = self._db.reprocessing_runs.get(run_id)
        if run is None or not self._scope.permits(run.organization_id):
            return None
        settle(run, outcome, tokens=tokens)
        return run

    async def rewrite_embedding_model(self, embedding_model: str) -> int:
        from app.services.index_fingerprint import with_embedding_model

        changed = 0
        for document in self._db.documents.values():
            if (
                document.status == "indexed"
                and document.index_fingerprint is not None
                and self._scope.permits(document.organization_id)
            ):
                document.embedding_model = embedding_model
                document.index_fingerprint = with_embedding_model(
                    document.index_fingerprint, embedding_model
                )
                changed += 1
        return changed

    async def commit(self) -> None:
        return None


@dataclass
class MemoryConnectorStore:
    database: MemoryDatabase = field(default_factory=MemoryDatabase)

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[ConnectorTransaction]:
        yield MemoryConnectorTransaction(self.database, scope)


__all__ = [
    "ConnectorStore",
    "ConnectorTransaction",
    "DocumentAuditRow",
    "DocumentDraft",
    "MemoryConnectorStore",
    "MemoryConnectorTransaction",
    "PostgresConnectorStore",
    "PostgresConnectorTransaction",
    "ReprocessScope",
    "StaleCounts",
    "StaleSummary",
]
