"""Reading the audit log, and the one write that has no mutation to ride along with.

The recording side of task 15 does not live here: an event is added to whichever session
is already performing the change, through the mixins in :mod:`app.services.audit`, so
that a committed mutation cannot lack its record. What this module owns is the *read*
half — the screen, the contextual tabs, the CSV export — plus :meth:`AuditStore.append`,
which exists for the single event that has no mutation to attach to.

That event is a superadmin opening a customer's organization. SPEC §5.2 requires it to be
recorded, and it happens on a *read* — the first request of a support session — so there
is no unit of work to join. It is written on its own, off the request path, and the
debounce that decides when is in :mod:`app.services.impersonation`.

**Nothing here can update or delete.** There is no method for it, and the table's trigger
refuses one anyway (see the ``0014_audit_log`` migration). Both halves are deliberate: the
missing method is what a reviewer sees, and the trigger is what holds when somebody adds
one.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.tenancy import TenantScope
from app.db.models import AuditEvent
from app.db.repositories import AuditEventRepository
from app.services.memory_db import MemoryDatabase


@dataclass(frozen=True, slots=True)
class AuditFilters:
    """What the audit screen is asking about.

    Every field optional, unlike the request log's window. The two tables are read for
    opposite reasons: monitoring answers "what is happening now", where an unbounded query
    would scan every partition retained, and this one answers "when did this change", where
    the honest default is the whole history — the table gains a few thousand rows a year,
    and a default window would hide the six-month-old change somebody is looking for.
    """

    #: For a platform caller narrowing to one customer. For anyone else it can only
    #: intersect with their own scope, so it can never widen anything.
    organization_id: uuid.UUID | None = None
    actor_user_id: uuid.UUID | None = None
    action: str | None = None
    target_type: str | None = None
    target_id: uuid.UUID | None = None
    start: datetime | None = None
    end: datetime | None = None


class AuditReader(Protocol):
    """One scoped read. Deliberately narrow: one question, and no way to write.

    There is no ``event(id)`` here because nothing reads one event by id — the screen
    lists, the contextual panel lists with a target filter, and the export lists. A method
    with no caller is a claim no test can keep honest.
    """

    async def events(
        self, filters: AuditFilters, *, after: uuid.UUID | None, limit: int
    ) -> Sequence[AuditEvent]:
        """Newest first, over-fetched by one so the caller can page."""


class AuditStore(Protocol):
    def begin(self, scope: TenantScope) -> AbstractAsyncContextManager[AuditReader]:
        """Open a scoped read: ``async with store.begin(scope) as reader``."""
        ...

    async def append(self, event: AuditEvent) -> None:
        """Write one event in its own transaction.

        Only for an event with no mutation to join — see the module docstring. Everything
        else records through the transaction that is already changing something.
        """


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


class PostgresAuditReader:
    def __init__(self, session: AsyncSession, scope: TenantScope) -> None:
        self._session = session
        self._events = AuditEventRepository(session, scope)

    async def events(
        self, filters: AuditFilters, *, after: uuid.UUID | None, limit: int
    ) -> Sequence[AuditEvent]:
        statement: Select[tuple[AuditEvent]] = self._events.page(filters, after=after, limit=limit)
        return (await self._session.execute(statement)).scalars().all()


class PostgresAuditStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[AuditReader]:
        async with self._session_factory() as session:
            yield PostgresAuditReader(session, scope)

    async def append(self, event: AuditEvent) -> None:
        async with self._session_factory() as session:
            session.add(event)
            await session.commit()


# ---------------------------------------------------------------------------
# in memory
# ---------------------------------------------------------------------------


class MemoryAuditReader:
    """The same filters, applied to dictionaries through
    :meth:`~app.core.tenancy.TenantScope.permits` — the row-level twin of the SQL clause
    the repository builds."""

    def __init__(self, database: MemoryDatabase, scope: TenantScope) -> None:
        self._db = database
        self._scope = scope

    async def events(
        self, filters: AuditFilters, *, after: uuid.UUID | None, limit: int
    ) -> Sequence[AuditEvent]:
        rows = [row for row in self._db.audit_events.values() if self._visible(row)]
        rows = [row for row in rows if _matches(row, filters)]
        # Newest first, matching ORDER BY id DESC — UUIDv7 ids sort by creation time.
        rows.sort(key=lambda row: row.id, reverse=True)
        if after is not None:
            rows = [row for row in rows if row.id < after]
        return rows[: limit + 1]

    def _visible(self, row: AuditEvent) -> bool:
        if row.organization_id is None:
            # A platform event belongs to no customer. `permits(None)` says exactly that.
            return self._scope.is_platform
        return self._scope.permits(row.organization_id)


def _matches(row: AuditEvent, filters: AuditFilters) -> bool:
    if filters.organization_id is not None and row.organization_id != filters.organization_id:
        return False
    if filters.actor_user_id is not None and row.actor_user_id != filters.actor_user_id:
        return False
    if filters.action is not None and row.action != filters.action:
        return False
    if filters.target_type is not None and row.target_type != filters.target_type:
        return False
    if filters.target_id is not None and row.target_id != filters.target_id:
        return False
    if filters.start is not None and row.created_at < filters.start:
        return False
    return not (filters.end is not None and row.created_at >= filters.end)


class MemoryAuditStore:
    def __init__(self, database: MemoryDatabase | None = None) -> None:
        self._db = database or MemoryDatabase()

    @property
    def database(self) -> MemoryDatabase:
        return self._db

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[AuditReader]:
        yield MemoryAuditReader(self._db, scope)

    async def append(self, event: AuditEvent) -> None:
        self._db.add_audit_event(event)


__all__ = [
    "AuditFilters",
    "AuditReader",
    "AuditStore",
    "MemoryAuditReader",
    "MemoryAuditStore",
    "PostgresAuditReader",
    "PostgresAuditStore",
]
