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
from typing import Protocol

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import uuid7
from app.core.tenancy import TenantScope
from app.db.models import Connector, Document
from app.db.repositories import ConnectorRepository, DocumentIndexRow, DocumentRepository
from app.db.scoping import scoped
from app.services.memory_db import MemoryDatabase


@dataclass(frozen=True, slots=True)
class DocumentDraft:
    """What is known about an object before anything has read it."""

    source_uri: str
    source_name: str
    size_bytes: int = 0
    etag: str | None = None
    mime_type: str | None = None


class ConnectorTransaction(Protocol):
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
    ) -> Sequence[Document]: ...

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

    async def commit(self) -> None: ...


class ConnectorStore(Protocol):
    def begin(self, scope: TenantScope) -> AbstractAsyncContextManager[ConnectorTransaction]:
        """Open a scoped transaction: ``async with store.begin(scope) as tx``."""
        ...


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


class PostgresConnectorTransaction:
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
    ) -> Sequence[Document]:
        return await self._documents.fetch(
            self._documents.page(connector_id, after=after, limit=limit, status=status)
        )

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

    async def commit(self) -> None:
        await self._session.commit()


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


class MemoryConnectorTransaction:
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
    ) -> Sequence[Document]:
        rows = [
            document
            for document in self._db.documents.values()
            if document.connector_id == connector_id
            and self._scope.permits(document.organization_id)
            and (status is None or document.status == status)
        ]
        ordered = sorted(rows, key=lambda row: row.id, reverse=True)
        if after is not None:
            ordered = [row for row in ordered if row.id < after]
        return ordered[: limit + 1]

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
    "DocumentDraft",
    "MemoryConnectorStore",
    "MemoryConnectorTransaction",
    "PostgresConnectorStore",
    "PostgresConnectorTransaction",
]
