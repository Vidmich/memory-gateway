"""Where a job goes when it has run out of retries.

Separate from :mod:`app.services.jobs` because that module is pure — no SQLAlchemy, no
session — which is what lets the retry policy be tested exhaustively without a database.
This is the one adapter that writes.

The insert is unscoped, and deliberately so: ``job_dead_letters`` carries no
``organization_id``, so there is no tenant to scope it by. See the model docstring for
why that column does not exist.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.ids import uuid7
from app.db.models import JobDeadLetter
from app.services.jobs import JobRequest

logger = logging.getLogger(__name__)

#: Cap on what is stored. A payload is a handful of ids today, but a job type added later
#: could carry something larger, and a dead letter is diagnostic — not a backup of the
#: work.
MAX_ERROR_CHARS = 4000


class PostgresDeadLetters:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, request: JobRequest, *, reason: str) -> None:
        async with self._session_factory() as session:
            session.add(
                JobDeadLetter(
                    id=uuid7(),
                    job_name=request.name[:64],
                    idempotency_key=request.idempotency_key,
                    payload=dict(request.payload),
                    attempts=max(1, request.attempt),
                    error=reason[:MAX_ERROR_CHARS],
                    request_id=request.request_id[:64] if request.request_id else None,
                )
            )
            await session.commit()


__all__ = ["MAX_ERROR_CHARS", "PostgresDeadLetters"]
