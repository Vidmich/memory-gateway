"""Where platform settings live, and the audit trail that comes with changing one.

One row per section (see :mod:`app.schemas.platform`), read as a whole and written as a
whole. There is no partial write below this line: a PATCH that changes two sections writes
two rows in one transaction, because the alternative — a screen that saved the embedding
model and then failed to save the dimension — is the failure mode the section grouping
exists to prevent.

The events go into the **platform's** log rather than any organization's: an
``organization_id`` of ``NULL`` is what SPEC §10.4 already uses for a superadmin acting
outside a tenant, and putting a platform-wide change into one customer's log would be both
wrong and, for the other customers, invisible.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import PlatformSetting, User
from app.db.scoping import unscoped
from app.services.audit import (
    Attribution,
    AuditingTransaction,
    MemoryAuditRecorder,
    PostgresAuditRecorder,
)
from app.services.memory_db import MemoryDatabase


@dataclass(frozen=True, slots=True)
class StoredSetting:
    """One section as it sits in the database, with its attribution.

    ``label`` is the author's email resolved at read time rather than denormalised onto
    the row. Unlike an audit event — which must stay readable after the account is gone —
    this is a "who to ask about the current value" field, and asking about a value set by
    a deleted account is a question with no useful answer anyway.
    """

    key: str
    value: Any
    updated_at: datetime
    updated_by: uuid.UUID | None = None
    label: str | None = None


class PlatformSettingsTransaction(AuditingTransaction, Protocol):
    async def all(self) -> list[StoredSetting]: ...

    async def put(self, key: str, value: Any, *, updated_by: uuid.UUID | None) -> None:
        """Insert or replace one section. Replaces rather than merges — merging is the
        caller's job, and doing it here as well would be two places that can disagree
        about what a partial update means."""
        ...

    async def commit(self) -> None: ...


class PlatformSettingsStore(Protocol):
    def begin(self) -> AbstractAsyncContextManager[PlatformSettingsTransaction]: ...


# ---------------------------------------------------------------------------
# postgres
# ---------------------------------------------------------------------------

#: Why these statements span no tenant. Platform settings have no ``organization_id`` at
#: all, so the guard never sees them; the join to ``users`` for an author's email does,
#: and it is one row looked up by primary key.
_REASON = "platform settings belong to the operator, not to any organization"


class PostgresPlatformSettingsTransaction(PostgresAuditRecorder):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def all(self) -> list[StoredSetting]:
        rows = await self._session.execute(
            select(PlatformSetting, User.email)
            .join(User, User.id == PlatformSetting.updated_by, isouter=True)
            .order_by(PlatformSetting.key)
            .execution_options(**unscoped(_REASON))
        )
        return [
            StoredSetting(
                key=setting.key,
                value=setting.value,
                updated_at=setting.updated_at,
                updated_by=setting.updated_by,
                label=email,
            )
            for setting, email in rows.all()
        ]

    async def put(self, key: str, value: Any, *, updated_by: uuid.UUID | None) -> None:
        statement = pg_insert(PlatformSetting).values(
            key=key, value=value, updated_by=updated_by, updated_at=datetime.now(UTC)
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[PlatformSetting.key],
                set_={
                    "value": statement.excluded.value,
                    "updated_by": statement.excluded.updated_by,
                    "updated_at": statement.excluded.updated_at,
                },
            ).execution_options(**unscoped(_REASON))
        )

    async def commit(self) -> None:
        await self._session.commit()


class PostgresPlatformSettingsStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[PlatformSettingsTransaction]:
        async with self._session_factory() as session:
            yield PostgresPlatformSettingsTransaction(session)


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


@dataclass
class MemoryPlatformSettingsTransaction(MemoryAuditRecorder):
    _db: MemoryDatabase
    _pending: dict[str, StoredSetting] = field(default_factory=dict)

    async def all(self) -> list[StoredSetting]:
        return [self._db.platform_settings[key] for key in sorted(self._db.platform_settings)]

    async def put(self, key: str, value: Any, *, updated_by: uuid.UUID | None) -> None:
        self._pending[key] = StoredSetting(
            key=key, value=value, updated_at=datetime.now(UTC), updated_by=updated_by
        )

    async def commit(self) -> None:
        self._db.platform_settings.update(self._pending)
        self._pending.clear()


@dataclass
class MemoryPlatformSettingsStore:
    db: MemoryDatabase

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[PlatformSettingsTransaction]:
        yield MemoryPlatformSettingsTransaction(self.db)


def platform_attribution(job: str) -> Attribution:
    """A background job's attribution at platform scope.

    Its own function because ``Attribution.system(None, job=...)`` reads as though the
    ``None`` were an oversight, and every scheduled job in this task needs one.
    """
    return Attribution.system(None, job=job)


def as_json(value: Any) -> Any:
    """Whatever a settings section is, in a form JSONB will take."""
    if isinstance(value, Mapping):
        return {str(key): as_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [as_json(item) for item in value]
    if isinstance(value, uuid.UUID | datetime):
        return str(value)
    return value


__all__ = [
    "MemoryPlatformSettingsStore",
    "MemoryPlatformSettingsTransaction",
    "PlatformSettingsStore",
    "PlatformSettingsTransaction",
    "PostgresPlatformSettingsStore",
    "PostgresPlatformSettingsTransaction",
    "StoredSetting",
    "as_json",
    "platform_attribution",
]
