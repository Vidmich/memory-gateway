"""Persistence for end users and their facts, behind a port.

Same shape as :mod:`app.services.connector_store` — a scoped transaction, a PostgreSQL
implementation and an in-memory twin that a contract test runs the same assertions
against — with two methods that are unlike anything in the earlier stores because they
are the only store calls this system makes **on the serving path**.

:meth:`EndUserTransaction.touch` is an upsert, not a read-then-insert, for the same
reason ``claim_document`` is: two requests from a brand-new end user arriving at once
would otherwise produce either a duplicate row or an integrity error depending on the
timing. ``ON CONFLICT DO UPDATE`` with a no-op assignment is what makes ``RETURNING``
yield the existing row as well as a newly inserted one, so the whole thing is one round
trip whichever it was.

:meth:`EndUserTransaction.live_facts` and :meth:`EndUserTransaction.recent_facts` are
recall's two reads, and both carry the *same* liveness predicate: not superseded, not
expired. It is written once, in :func:`live_clause`, because a recall path that forgot
half of it would inject a fact the customer believes they retracted — which is the one
failure this feature cannot have. The in-memory twin evaluates the same predicate in
Python from the same helper, so a test that passes there is testing the same rule.

Nothing here knows about vectors. Deleting a fact row and deleting its point are two
operations on two systems, sequenced by :mod:`app.services.end_users`, which is also
where the ordering — vectors first, then rows — is argued.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import ColumnElement, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import uuid7
from app.core.tenancy import TenantScope
from app.db.models import EndUser, MemoryFact
from app.db.scoping import ScopedRepository, scoped
from app.services.memory_db import MemoryDatabase


def live_clause(now: datetime) -> ColumnElement[bool]:
    """A fact that may still be injected: not superseded, not expired.

    One definition, used by both recall reads and by the browser's "live" filter. The
    expiry half is ``NULL OR in the future`` rather than a bare comparison, because a null
    ``expires_at`` is the common case and ``NULL > now()`` is null, not true — a mistake
    that would silently stop injecting every fact that has no expiry at all.
    """
    return and_(
        MemoryFact.superseded_at.is_(None),
        or_(MemoryFact.expires_at.is_(None), MemoryFact.expires_at > now),
    )


def _new_fact(end_user: EndUser, draft: FactDraft) -> MemoryFact:
    """The row both implementations insert, built once so they cannot disagree."""
    now = datetime.now(UTC)
    return MemoryFact(
        id=uuid7(),
        organization_id=end_user.organization_id,
        end_user_id=end_user.id,
        text=draft.text,
        kind=draft.kind,
        confidence=draft.confidence,
        expires_at=draft.expires_at,
        source_log_id=draft.source_log_id,
        created_at=now,
        last_seen_at=now,
    )


def is_live(fact: MemoryFact, now: datetime) -> bool:
    """The same predicate, against a row in hand. Two lines from :func:`live_clause` so
    the two cannot drift; ``tests/end_user_store_contract.py`` runs both."""
    if fact.superseded_at is not None:
        return False
    return fact.expires_at is None or fact.expires_at > now


@dataclass(frozen=True, slots=True)
class FactDraft:
    """A fact about to be written, before it has an id or a vector."""

    text: str
    kind: str = "fact"
    confidence: float = 1.0
    expires_at: datetime | None = None
    source_log_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class FactPatch:
    """A partial edit. ``None`` means "leave it alone" for every field, which is why
    clearing an expiry is :attr:`clear_expiry` rather than ``expires_at=None``."""

    text: str | None = None
    kind: str | None = None
    confidence: float | None = None
    expires_at: datetime | None = None
    clear_expiry: bool = False
    superseded: bool | None = None


class EndUserTransaction(Protocol):
    """One unit of work, already scoped. Returned objects are live in both
    implementations: mutate one and commit."""

    @property
    def scope(self) -> TenantScope: ...

    # -- identity ---------------------------------------------------------

    async def touch(self, external_id: str) -> EndUser:
        """The row for this identity, creating it if this is the first sight.

        One statement. See the module docstring for why it is an upsert and why the
        conflict branch assigns a column to itself.
        """
        ...

    async def end_user(self, end_user_id: uuid.UUID) -> EndUser | None: ...

    async def by_external_id(self, external_id: str) -> EndUser | None: ...

    async def end_users(
        self, *, after: uuid.UUID | None, limit: int, search: str | None = None
    ) -> Sequence[EndUser]: ...

    async def bump(self, counts: Mapping[uuid.UUID, int], *, seen_at: datetime) -> int:
        """Add to ``request_count`` and move ``last_seen_at`` forward, in one batch.

        Returns how many rows were updated, which is not always ``len(counts)``: an end
        user purged between the request and the flush is simply gone, and that is not an
        error worth raising on a background task.
        """
        ...

    # -- facts ------------------------------------------------------------

    async def fact(self, fact_id: uuid.UUID) -> MemoryFact | None: ...

    async def facts(
        self,
        end_user_id: uuid.UUID,
        *,
        after: uuid.UUID | None,
        limit: int,
        live_only: bool = False,
    ) -> Sequence[MemoryFact]: ...

    async def add_fact(self, end_user: EndUser, draft: FactDraft) -> MemoryFact: ...

    async def delete_fact(self, fact: MemoryFact) -> None: ...

    async def delete_facts_of(self, end_user_id: uuid.UUID) -> int:
        """Every fact for one end user, gone. The erasure path; returns the row count."""
        ...

    async def fact_counts(self, end_user_ids: Sequence[uuid.UUID]) -> Mapping[uuid.UUID, int]:
        """Live facts per end user, for the list screen. One query for the whole page."""
        ...

    async def count_facts(self, end_user_id: uuid.UUID, *, live_only: bool = True) -> int: ...

    async def live_facts(
        self, end_user_id: uuid.UUID, fact_ids: Sequence[uuid.UUID], *, now: datetime
    ) -> Sequence[MemoryFact]:
        """The rows behind a vector search's hits, keeping only the ones still believed.

        ``end_user_id`` is passed as well as the ids, and it is not redundant: the ids
        came from a vector store, and a filter there being wrong must not be able to
        become a disclosure here. Two independent checks of the same rule is the whole
        point.
        """
        ...

    async def recent_facts(
        self,
        end_user_id: uuid.UUID,
        *,
        limit: int,
        min_confidence: float,
        now: datetime,
    ) -> Sequence[MemoryFact]:
        """The most recently seen high-confidence facts, regardless of similarity.

        SPEC's always-include set. "Uses metric units" and "is a minor" have to reach
        every turn, and a dense search for "how do I return this" will not find either.
        """
        ...

    async def commit(self) -> None: ...


class EndUserStore(Protocol):
    def begin(self, scope: TenantScope) -> AbstractAsyncContextManager[EndUserTransaction]:
        """Open a scoped transaction: ``async with store.begin(scope) as tx``."""
        ...


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


class EndUserRepository(ScopedRepository[EndUser]):
    model = EndUser


class MemoryFactRepository(ScopedRepository[MemoryFact]):
    model = MemoryFact


class PostgresEndUserTransaction:
    def __init__(self, session: AsyncSession, scope: TenantScope) -> None:
        self._session = session
        self._scope = scope
        self._end_users = EndUserRepository(session, scope)
        self._facts = MemoryFactRepository(session, scope)

    @property
    def scope(self) -> TenantScope:
        return self._scope

    # -- identity ---------------------------------------------------------

    async def touch(self, external_id: str) -> EndUser:
        organization_id = self._scope.require_organization()
        now = datetime.now(UTC)
        statement = (
            pg_insert(EndUser)
            .values(
                id=uuid7(),
                organization_id=organization_id,
                external_id=external_id,
                first_seen_at=now,
                last_seen_at=now,
                request_count=0,
            )
            .on_conflict_do_update(
                constraint="uq_end_users_organization_id_external_id",
                # Assigning the conflict key to itself. The row is not changed; the
                # `DO UPDATE` branch is what makes PostgreSQL return a row at all, and
                # `DO NOTHING` would need a second round trip to read the existing one.
                # The counters are deliberately untouched here — they are batched.
                set_={"external_id": external_id},
            )
            .returning(EndUser.id)
            .execution_options(**scoped())
        )
        end_user_id = (await self._session.execute(statement)).scalar_one()
        found = await self._end_users.get(end_user_id)
        if found is None:  # pragma: no cover - the upsert above guarantees a row
            raise RuntimeError(f"end user row vanished for {external_id!r}")
        return found

    async def end_user(self, end_user_id: uuid.UUID) -> EndUser | None:
        return await self._end_users.get(end_user_id)

    async def by_external_id(self, external_id: str) -> EndUser | None:
        statement = self._end_users.select().where(EndUser.external_id == external_id)
        return (await self._session.execute(statement)).scalars().first()

    async def end_users(
        self, *, after: uuid.UUID | None, limit: int, search: str | None = None
    ) -> Sequence[EndUser]:
        statement = self._end_users.select().order_by(EndUser.id.desc()).limit(limit + 1)
        if search:
            # A prefix-and-substring match on the id somebody remembers. `ilike` rather
            # than a trigram index: the table is small relative to `request_logs`, and a
            # search box on an admin screen is not a hot path.
            statement = statement.where(EndUser.external_id.ilike(f"%{_escaped(search)}%"))
        if after is not None:
            statement = statement.where(EndUser.id < after)
        return (await self._session.execute(statement)).scalars().all()

    async def bump(self, counts: Mapping[uuid.UUID, int], *, seen_at: datetime) -> int:
        if not counts:
            return 0
        total = 0
        for end_user_id, amount in counts.items():
            statement = (
                update(EndUser)
                .where(self._scope.clause(EndUser), EndUser.id == end_user_id)
                .values(
                    request_count=EndUser.request_count + amount,
                    # `greatest` rather than a plain assignment: two replicas flushing
                    # out of order must not move the timestamp backwards.
                    last_seen_at=func.greatest(EndUser.last_seen_at, seen_at),
                )
                .execution_options(**scoped())
            )
            result = await self._session.execute(statement)
            # `rowcount` is on the cursor result an UPDATE returns; the generic
            # `Result` type does not declare it, and a `getattr` says that out loud
            # rather than hiding it behind a cast.
            total += int(getattr(result, "rowcount", 0) or 0)
        return total

    # -- facts ------------------------------------------------------------

    async def fact(self, fact_id: uuid.UUID) -> MemoryFact | None:
        return await self._facts.get(fact_id)

    async def facts(
        self,
        end_user_id: uuid.UUID,
        *,
        after: uuid.UUID | None,
        limit: int,
        live_only: bool = False,
    ) -> Sequence[MemoryFact]:
        statement = (
            self._facts.select()
            .where(MemoryFact.end_user_id == end_user_id)
            .order_by(MemoryFact.id.desc())
            .limit(limit + 1)
        )
        if live_only:
            statement = statement.where(live_clause(datetime.now(UTC)))
        if after is not None:
            statement = statement.where(MemoryFact.id < after)
        return (await self._session.execute(statement)).scalars().all()

    async def add_fact(self, end_user: EndUser, draft: FactDraft) -> MemoryFact:
        # The tenant comes from the *end user*, not from the scope, and that is not the
        # usual pattern here: `ScopedRepository.add` stamps the caller's organization,
        # which is right for a resource a caller creates from nothing. A fact is about a
        # person who already belongs somewhere, and the row it was read through was
        # already scoped — so taking the value from there makes a fact in the wrong
        # organization impossible rather than merely unlikely.
        row = _new_fact(end_user, draft)
        self._session.add(row)
        await self._session.flush()
        return row

    async def delete_fact(self, fact: MemoryFact) -> None:
        await self._facts.delete(fact)

    async def delete_facts_of(self, end_user_id: uuid.UUID) -> int:
        rows = await self._facts.fetch(
            self._facts.select().where(MemoryFact.end_user_id == end_user_id)
        )
        for row in rows:
            await self._session.delete(row)
        await self._session.flush()
        return len(rows)

    async def fact_counts(self, end_user_ids: Sequence[uuid.UUID]) -> Mapping[uuid.UUID, int]:
        if not end_user_ids:
            return {}
        statement = (
            select(MemoryFact.end_user_id, func.count())
            .where(
                self._scope.clause(MemoryFact),
                MemoryFact.end_user_id.in_(end_user_ids),
                live_clause(datetime.now(UTC)),
            )
            .group_by(MemoryFact.end_user_id)
            .execution_options(**scoped())
        )
        return {row[0]: int(row[1]) for row in (await self._session.execute(statement)).all()}

    async def count_facts(self, end_user_id: uuid.UUID, *, live_only: bool = True) -> int:
        statement = (
            select(func.count())
            .select_from(MemoryFact)
            .where(self._scope.clause(MemoryFact), MemoryFact.end_user_id == end_user_id)
            .execution_options(**scoped())
        )
        if live_only:
            statement = statement.where(live_clause(datetime.now(UTC)))
        return int((await self._session.execute(statement)).scalar() or 0)

    async def live_facts(
        self, end_user_id: uuid.UUID, fact_ids: Sequence[uuid.UUID], *, now: datetime
    ) -> Sequence[MemoryFact]:
        if not fact_ids:
            return []
        statement = self._facts.select().where(
            MemoryFact.end_user_id == end_user_id,
            MemoryFact.id.in_(list(fact_ids)),
            live_clause(now),
        )
        return (await self._session.execute(statement)).scalars().all()

    async def recent_facts(
        self,
        end_user_id: uuid.UUID,
        *,
        limit: int,
        min_confidence: float,
        now: datetime,
    ) -> Sequence[MemoryFact]:
        statement = (
            self._facts.select()
            .where(
                MemoryFact.end_user_id == end_user_id,
                MemoryFact.confidence >= min_confidence,
                live_clause(now),
            )
            .order_by(MemoryFact.last_seen_at.desc(), MemoryFact.id.desc())
            .limit(limit)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def commit(self) -> None:
        await self._session.commit()


class PostgresEndUserStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[EndUserTransaction]:
        async with self._session_factory() as session:
            yield PostgresEndUserTransaction(session, scope)


def _escaped(value: str) -> str:
    """Neutralise the wildcards in a ``LIKE`` pattern.

    Without this a search for ``%`` matches every end user in the organization, which is
    not a security hole — the scope still holds — but is a search box that lies.
    """
    return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


# ---------------------------------------------------------------------------
# in memory
# ---------------------------------------------------------------------------


class MemoryEndUserTransaction:
    def __init__(self, database: MemoryDatabase, scope: TenantScope) -> None:
        self._db = database
        self._scope = scope

    @property
    def scope(self) -> TenantScope:
        return self._scope

    # -- identity ---------------------------------------------------------

    async def touch(self, external_id: str) -> EndUser:
        existing = await self.by_external_id(external_id)
        if existing is not None:
            return existing
        now = datetime.now(UTC)
        return self._db.add_end_user(
            EndUser(
                id=uuid7(),
                organization_id=self._scope.require_organization(),
                external_id=external_id,
                first_seen_at=now,
                last_seen_at=now,
                request_count=0,
            )
        )

    async def end_user(self, end_user_id: uuid.UUID) -> EndUser | None:
        found = self._db.end_users.get(end_user_id)
        if found is None or not self._scope.permits(found.organization_id):
            return None
        return found

    async def by_external_id(self, external_id: str) -> EndUser | None:
        for row in self._db.end_users.values():
            if row.external_id == external_id and self._scope.permits(row.organization_id):
                return row
        return None

    async def end_users(
        self, *, after: uuid.UUID | None, limit: int, search: str | None = None
    ) -> Sequence[EndUser]:
        rows = [
            row for row in self._db.end_users.values() if self._scope.permits(row.organization_id)
        ]
        if search:
            needle = search.lower()
            rows = [row for row in rows if needle in row.external_id.lower()]
        ordered = sorted(rows, key=lambda row: row.id, reverse=True)
        if after is not None:
            ordered = [row for row in ordered if row.id < after]
        return ordered[: limit + 1]

    async def bump(self, counts: Mapping[uuid.UUID, int], *, seen_at: datetime) -> int:
        updated = 0
        for end_user_id, amount in counts.items():
            row = await self.end_user(end_user_id)
            if row is None:
                continue
            row.request_count += amount
            row.last_seen_at = max(row.last_seen_at, seen_at)
            updated += 1
        return updated

    # -- facts ------------------------------------------------------------

    async def fact(self, fact_id: uuid.UUID) -> MemoryFact | None:
        found = self._db.memory_facts.get(fact_id)
        if found is None or not self._scope.permits(found.organization_id):
            return None
        return found

    def _own_facts(self, end_user_id: uuid.UUID) -> list[MemoryFact]:
        return [
            fact
            for fact in self._db.memory_facts.values()
            if fact.end_user_id == end_user_id and self._scope.permits(fact.organization_id)
        ]

    async def facts(
        self,
        end_user_id: uuid.UUID,
        *,
        after: uuid.UUID | None,
        limit: int,
        live_only: bool = False,
    ) -> Sequence[MemoryFact]:
        now = datetime.now(UTC)
        rows = self._own_facts(end_user_id)
        if live_only:
            rows = [fact for fact in rows if is_live(fact, now)]
        ordered = sorted(rows, key=lambda row: row.id, reverse=True)
        if after is not None:
            ordered = [row for row in ordered if row.id < after]
        return ordered[: limit + 1]

    async def add_fact(self, end_user: EndUser, draft: FactDraft) -> MemoryFact:
        return self._db.add_fact(_new_fact(end_user, draft))

    async def delete_fact(self, fact: MemoryFact) -> None:
        self._db.memory_facts.pop(fact.id, None)

    async def delete_facts_of(self, end_user_id: uuid.UUID) -> int:
        rows = self._own_facts(end_user_id)
        for fact in rows:
            self._db.memory_facts.pop(fact.id, None)
        return len(rows)

    async def fact_counts(self, end_user_ids: Sequence[uuid.UUID]) -> Mapping[uuid.UUID, int]:
        now = datetime.now(UTC)
        wanted = set(end_user_ids)
        counts: dict[uuid.UUID, int] = {}
        for fact in self._db.memory_facts.values():
            if (
                fact.end_user_id in wanted
                and self._scope.permits(fact.organization_id)
                and is_live(fact, now)
            ):
                counts[fact.end_user_id] = counts.get(fact.end_user_id, 0) + 1
        return counts

    async def count_facts(self, end_user_id: uuid.UUID, *, live_only: bool = True) -> int:
        now = datetime.now(UTC)
        rows = self._own_facts(end_user_id)
        return sum(1 for fact in rows if not live_only or is_live(fact, now))

    async def live_facts(
        self, end_user_id: uuid.UUID, fact_ids: Sequence[uuid.UUID], *, now: datetime
    ) -> Sequence[MemoryFact]:
        wanted = set(fact_ids)
        return [
            fact
            for fact in self._own_facts(end_user_id)
            if fact.id in wanted and is_live(fact, now)
        ]

    async def recent_facts(
        self,
        end_user_id: uuid.UUID,
        *,
        limit: int,
        min_confidence: float,
        now: datetime,
    ) -> Sequence[MemoryFact]:
        rows = [
            fact
            for fact in self._own_facts(end_user_id)
            if fact.confidence >= min_confidence and is_live(fact, now)
        ]
        rows.sort(key=lambda fact: (fact.last_seen_at, fact.id), reverse=True)
        return rows[:limit]

    async def commit(self) -> None:
        return None


@dataclass
class MemoryEndUserStore:
    database: MemoryDatabase = field(default_factory=MemoryDatabase)

    @asynccontextmanager
    async def begin(self, scope: TenantScope) -> AsyncIterator[EndUserTransaction]:
        yield MemoryEndUserTransaction(self.database, scope)


__all__ = [
    "EndUserRepository",
    "EndUserStore",
    "EndUserTransaction",
    "FactDraft",
    "FactPatch",
    "MemoryEndUserStore",
    "MemoryEndUserTransaction",
    "MemoryFactRepository",
    "PostgresEndUserStore",
    "PostgresEndUserTransaction",
    "is_live",
    "live_clause",
]
