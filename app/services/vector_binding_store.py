"""Reading and writing the row that says where an organization's vectors are.

Small, and separated from :mod:`app.services.vector_backends` on purpose: the resolver
above it is on the retrieval path of every request that uses memory, and the thing that
makes it fast is a cache in front of exactly this interface. Keeping the interface narrow
is what keeps the cache honest — five methods, three of which are writes that invalidate.

An **absent row is not an error**. It means "this organization has not been placed yet",
and the resolver answers with the platform default and writes the row on first use. That
is the same precedence rule task 17 established for platform settings: the environment (or
here, the default) is the bootstrap, the row is the answer, and nothing is backfilled at
migration time — because backfilling would freeze today's default into every existing
tenant and make a later change to it apply to nobody.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import VectorBinding
from app.db.scoping import scoped, unscoped


@dataclass(frozen=True, slots=True)
class Binding:
    """Where one organization's vectors live, and whether they are on the move.

    ``collection`` is the live physical collection for a backend that cannot answer that
    about itself, and stays ``None`` for one that can — see
    :mod:`app.db.models.vectors` for why recording it twice would be worse than not
    recording it at all.
    """

    organization_id: uuid.UUID
    backend: str
    collection: str | None = None
    status: str = "bound"
    target: str | None = None
    target_collection: str | None = None

    @property
    def migrating(self) -> bool:
        return self.status == "migrating"

    def reading_from(self) -> str:
        """The backend a *search* goes to right now.

        Always ``backend``, including mid-migration. That single line is the whole
        zero-downtime property: a copy being built in ``target`` is invisible to readers
        until it is promoted, so a migration that stalls, fails or is abandoned costs
        disk and nothing else.
        """
        return self.backend


class VectorBindingStore(Protocol):
    async def get(self, organization_id: uuid.UUID) -> Binding | None: ...

    async def put(self, binding: Binding) -> Binding:
        """Insert or replace the row. Replaces rather than merges: every caller here has
        the whole row in hand, and a merge would let a half-written migration survive a
        write that meant to end it."""
        ...

    async def forget(self, organization_id: uuid.UUID) -> None: ...

    async def all(self) -> list[Binding]:
        """Every binding. The platform screens and the orphan sweeper, which both need to
        know which organizations are on which backend."""
        ...

    async def on(self, backend: str) -> list[Binding]: ...


class PostgresVectorBindingStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, organization_id: uuid.UUID) -> Binding | None:
        async with self._sessions() as session:
            statement = (
                select(VectorBinding)
                .where(VectorBinding.organization_id == organization_id)
                .execution_options(**scoped())
            )
            row = (await session.execute(statement)).scalar_one_or_none()
            return None if row is None else _binding(row)

    async def put(self, binding: Binding) -> Binding:
        values = {
            "organization_id": binding.organization_id,
            "backend": binding.backend,
            "collection": binding.collection,
            "status": binding.status,
            "target": binding.target,
            "target_collection": binding.target_collection,
            "updated_at": datetime.now(UTC),
        }
        async with self._sessions() as session, session.begin():
            statement = (
                pg_insert(VectorBinding)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[VectorBinding.organization_id],
                    set_={key: value for key, value in values.items() if key != "organization_id"},
                )
                # An upsert keyed by the organization id is as scoped as a statement gets;
                # the guard cannot see that through `ON CONFLICT`, so it is declared.
                .execution_options(**scoped())
            )
            await session.execute(statement)
        return binding

    async def forget(self, organization_id: uuid.UUID) -> None:
        async with self._sessions() as session, session.begin():
            row = await session.get(VectorBinding, organization_id)
            if row is not None:
                await session.delete(row)

    async def all(self) -> list[Binding]:
        async with self._sessions() as session:
            statement = select(VectorBinding).execution_options(
                **unscoped("the platform view of where every tenant's vectors live")
            )
            rows = (await session.execute(statement)).scalars().all()
            return [_binding(row) for row in rows]

    async def on(self, backend: str) -> list[Binding]:
        async with self._sessions() as session:
            statement = (
                select(VectorBinding)
                .where(VectorBinding.backend == backend)
                .execution_options(**unscoped("enumerating one backend's tenants for the sweeper"))
            )
            rows = (await session.execute(statement)).scalars().all()
            return [_binding(row) for row in rows]


def _binding(row: VectorBinding) -> Binding:
    return Binding(
        organization_id=row.organization_id,
        backend=row.backend,
        collection=row.collection,
        status=row.status,
        target=row.target,
        target_collection=row.target_collection,
    )


@dataclass
class MemoryVectorBindingStore:
    """A dict, for tests and for the single-backend wiring that needs no database."""

    rows: dict[uuid.UUID, Binding] = field(default_factory=dict)

    async def get(self, organization_id: uuid.UUID) -> Binding | None:
        return self.rows.get(organization_id)

    async def put(self, binding: Binding) -> Binding:
        self.rows[binding.organization_id] = binding
        return binding

    async def forget(self, organization_id: uuid.UUID) -> None:
        self.rows.pop(organization_id, None)

    async def all(self) -> list[Binding]:
        return list(self.rows.values())

    async def on(self, backend: str) -> list[Binding]:
        return [row for row in self.rows.values() if row.backend == backend]


def sole_backend(bindings: Sequence[Binding]) -> set[str]:
    """Every backend named by a set of bindings, migrations included.

    Used where a question is about *storage* rather than about reads — a backup, a health
    summary, an offboarding — and a tenant mid-migration genuinely occupies two.
    """
    named: set[str] = set()
    for binding in bindings:
        named.add(binding.backend)
        if binding.target is not None:
            named.add(binding.target)
    return named


__all__ = [
    "Binding",
    "MemoryVectorBindingStore",
    "PostgresVectorBindingStore",
    "VectorBindingStore",
    "sole_backend",
]
