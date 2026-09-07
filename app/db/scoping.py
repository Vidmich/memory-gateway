"""Tenant scoping, enforced in one place instead of forty.

Two mechanisms, and they are deliberately different in kind.

:class:`ScopedRepository` is the *rule*: every read it builds carries
``WHERE organization_id = :scope`` and every row it creates is stamped with the scope. A
domain repository inherits from it and gets that for free, so an endpoint written in task
09 is isolated because of where its data access lives, not because its author remembered.

The scope guard is the *backstop*. It watches ORM execution and refuses any statement
that touches a tenant-keyed table without either coming from a scoped repository or
saying, in the code, why it does not need to be scoped. Some queries genuinely must span
tenants — resolving a gateway by slug happens before anyone is authenticated, and login
looks a user up by email before it knows their organization. Those call :func:`unscoped`
with a reason, which is greppable, reviewable, and logged, rather than being invisible.

The guard fails closed. A statement that reaches it unmarked raises, in production as
well as in tests: a 500 on one endpoint is a smaller incident than one organization
reading another's rows.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import Select, event, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import ORMExecuteState, Session

from app.core.tenancy import TenantScope
from app.db.base import Base

logger = logging.getLogger(__name__)

#: The execution option every statement against a tenant-keyed table must carry.
SCOPE_OPTION = "tenant_scope"

#: Value meaning "a scoped repository built this".
SCOPED = "scoped"


class UnscopedQuery(RuntimeError):
    """A query touched a tenant-keyed table without declaring how it is scoped."""


def scoped() -> dict[str, Any]:
    return {SCOPE_OPTION: SCOPED}


def unscoped(reason: str) -> dict[str, Any]:
    """Execution options for a query that deliberately spans organizations.

    ``reason`` is not decoration. It is what a reviewer reads when deciding whether this
    particular hole is the one that lets a customer see another customer's data, and it
    is what ``grep -r 'unscoped('`` turns into a complete list of them.
    """
    if not reason:
        raise ValueError("an unscoped query must say why")
    return {SCOPE_OPTION: f"bypass:{reason}"}


def is_tenant_keyed(model: type[Any]) -> bool:
    """True for any mapped class carrying ``organization_id``.

    Membership is derived from the schema rather than from a hand-maintained list, so a
    table added in a later task is guarded the moment it declares the column.
    """
    try:
        mapper = inspect(model)
    except Exception:
        return False
    return mapper is not None and "organization_id" in mapper.columns


def guard_violation(
    models: Iterable[type[Any]],
    execution_options: Mapping[str, Any],
) -> str | None:
    """The failure message for a statement, or ``None`` if it is allowed.

    Pure, and separate from the event listener, because this is the part worth testing
    exhaustively and the listener is three lines of wiring around it.
    """
    keyed = sorted({model.__name__ for model in models if is_tenant_keyed(model)})
    if not keyed:
        return None

    declared = execution_options.get(SCOPE_OPTION)
    if declared:
        return None

    return (
        f"Query against tenant-keyed {', '.join(keyed)} carried no tenant scope. "
        "Read it through a ScopedRepository, or mark it with "
        "app.db.scoping.unscoped('why this must span organizations')."
    )


def install_scope_guard() -> None:
    """Register the guard on every ORM session in the process.

    Registered on the ``Session`` class rather than on one factory: a session created by
    a test, a CLI command, or a worker is exactly as capable of leaking as one created by
    a request.
    """
    if getattr(install_scope_guard, "_installed", False):
        return

    @event.listens_for(Session, "do_orm_execute")
    def _check(state: ORMExecuteState) -> None:
        # Relationship and deferred-column loads are emitted *by* a statement that was
        # already checked; re-checking them would demand a scope on a query the caller
        # never wrote.
        if state.is_relationship_load or state.is_column_load:
            return

        models = [mapper.class_ for mapper in state.all_mappers]
        problem = guard_violation(models, state.execution_options)
        if problem is None:
            return
        raise UnscopedQuery(problem)

    install_scope_guard._installed = True  # type: ignore[attr-defined]


class ScopedRepository[ModelT: Base]:
    """Base class for every repository over a tenant-keyed table.

    Subclasses set :attr:`model` and add domain methods that start from
    :meth:`select`. Nothing here reaches for the session directly except through the
    statements this class builds, which is what makes the scoping unconditional.
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession, scope: TenantScope) -> None:
        self._session = session
        self._scope = scope

    @property
    def scope(self) -> TenantScope:
        return self._scope

    def select(self) -> Select[tuple[ModelT]]:
        """A ``SELECT`` that is already restricted to the scope."""
        return (
            select(self.model).where(self._scope.clause(self.model)).execution_options(**scoped())
        )

    async def get(self, entity_id: uuid.UUID) -> ModelT | None:
        """Fetch by id, or ``None`` — including when the row exists but belongs to
        another organization. Callers turn that into a 404, which is why cross-tenant
        access cannot be distinguished from a typo."""
        statement = self.select().where(self.model.id == entity_id)  # type: ignore[attr-defined]
        return (await self._session.execute(statement)).scalars().first()

    async def fetch(self, statement: Select[tuple[ModelT]]) -> Sequence[ModelT]:
        return (await self._session.execute(statement)).scalars().all()

    async def add(self, entity: ModelT) -> ModelT:
        """Insert, stamping the scope's organization onto the row.

        A caller cannot choose the organization: the value is overwritten, not
        defaulted, so passing one has no effect rather than a surprising one.
        """
        entity.organization_id = self._scope.require_organization()  # type: ignore[attr-defined]
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def delete(self, entity: ModelT) -> None:
        await self._session.delete(entity)
        await self._session.flush()
