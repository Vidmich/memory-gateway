"""The connector abstraction: where content comes from, and nothing else.

SPEC §9.1 asks for this to be type-agnostic so that S3-external, SQL and HTTP connectors
slot in later "without changing the ingestion pipeline or the retrieval path". The test of
that is not the interface, it is what the pipeline imports: :mod:`app.services.ingestion`
knows about :class:`ConnectorSource` and never about object storage, which is why adding a
type is a class here plus a row in :data:`SOURCE_TYPES`.

Two deliberate departures from the signature in the task file.

**The protocol is called ``ConnectorSource``, not ``Connector``.** ``Connector`` is the
mapped row — the customer's configuration — and the two are genuinely different things: a
row is stored and edited, a source is constructed per job and reads bytes. Sharing a name
would make ``connector.fetch(...)`` ambiguous at every call site.

**``fetch`` yields chunks instead of returning ``BinaryIO``.** A synchronous file object
in an async pipeline leaves two options, and both are the bug this task's acceptance
criteria name: read it on the event loop and stall every other request on the process, or
read it whole and put a 50 MB file in worker memory. An async byte iterator is the same
idea with neither.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Protocol

from app.services.object_store import ObjectRef, ObjectStore

#: SPEC §9.1. The prefix a managed file-drop connector owns. Derived from ids, never from
#: anything a customer sends: a connector that could name its own prefix could name
#: another tenant's, and every isolation guarantee below this line rests on that.
STORAGE_PREFIX = "orgs/{organization_id}/connectors/{connector_id}/"


def storage_prefix(*, organization_id: uuid.UUID, connector_id: uuid.UUID) -> str:
    return STORAGE_PREFIX.format(organization_id=organization_id, connector_id=connector_id)


class ConnectorSource(Protocol):
    """Somewhere content comes from."""

    def list_objects(self) -> AsyncIterator[ObjectRef]:
        """Everything this connector currently holds. The input to reconciliation."""
        ...

    def fetch(self, ref: ObjectRef) -> AsyncIterator[bytes]:
        """The object's bytes, streamed. Raises :class:`KeyError` if it has gone."""
        ...

    def supports_push(self) -> bool:
        """Whether content can arrive without a resync.

        True for a managed file drop, because an upload enqueues its own ingestion. A
        pull-only source answers False, and the UI says "content appears after a sync"
        rather than offering an upload box that does nothing.
        """
        ...


class ManagedFileDropConnector:
    """SPEC §9.1's v1 type: a prefix in the platform's own object store.

    Holds the prefix rather than the connector row, so it cannot read outside it even if
    a caller passes a ref from somewhere else — :meth:`fetch` checks, because a ref is
    data and the whole isolation story would otherwise rest on every caller being careful.
    """

    type = "managed_file_drop"

    def __init__(self, store: ObjectStore, prefix: str) -> None:
        self._store = store
        self._prefix = prefix

    @property
    def prefix(self) -> str:
        return self._prefix

    def list_objects(self) -> AsyncIterator[ObjectRef]:
        return self._store.list(self._prefix)

    def fetch(self, ref: ObjectRef) -> AsyncIterator[bytes]:
        if not ref.key.startswith(self._prefix):
            raise PermissionError(f"{ref.key} is outside this connector")
        return self._store.open(ref.key)

    def supports_push(self) -> bool:
        return True


#: Every implemented type. The connector service refuses to build a source for anything
#: else, which is what turns "a row with a type this build does not know" into a readable
#: error rather than an ``AttributeError`` inside a worker.
SOURCE_TYPES = (ManagedFileDropConnector.type,)


def build_source(*, type_: str, store: ObjectStore, prefix: str | None) -> ConnectorSource:
    """The source for a stored connector row."""
    if type_ == ManagedFileDropConnector.type:
        if not prefix:
            # Only reachable for a row written outside the service, which is exactly when
            # a missing prefix would otherwise mean "list the whole bucket".
            raise ValueError("a managed file drop connector has no storage prefix")
        return ManagedFileDropConnector(store, prefix)
    raise ValueError(f"unsupported connector type: {type_}")


__all__ = [
    "SOURCE_TYPES",
    "STORAGE_PREFIX",
    "ConnectorSource",
    "ManagedFileDropConnector",
    "build_source",
    "storage_prefix",
]
