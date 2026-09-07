"""A mutual exclusion the whole deployment agrees on.

Used by one thing today — resync, so two of them do not interleave over one connector —
and deliberately small enough that adding the next user is obvious.

Redis rather than a PostgreSQL advisory lock, and the reason is about failure rather than
about preference. A session-level advisory lock is held by a *connection*: a worker that
is killed while holding one releases it when its connection is reaped, which is correct,
but a worker that hangs holds it indefinitely and there is no lease to expire. A Redis
``SET NX EX`` lock has a time-to-live, so the worst case is a delay rather than a
connector that can never be synced again until somebody finds the session.

The lock is a **convenience, not a correctness mechanism**, and the code that uses it is
written to be correct without it. Every distributed lock has a window where the holder has
stalled, the lease has expired, and two runners believe they hold it; a design that
depends on that never happening is a design that fails rarely and inexplicably. See
:meth:`~app.services.ingestion.IngestionPipeline.resync` for what actually keeps
concurrent reconciliation safe.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Long enough for a large resync, short enough that a dead holder is not a day's outage.
DEFAULT_TTL_SECONDS = 600

KEY_PREFIX = "lock:"


class Lock(Protocol):
    def hold(self, key: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Any:
        """``async with lock.hold(key) as held:`` — ``held`` is False if somebody else
        has it. Returning a flag rather than raising, because "somebody else is already
        doing this" is a normal outcome for every current caller."""
        ...


class RedisLock:
    def __init__(self, redis: Any) -> None:
        self._redis = redis

    @asynccontextmanager
    async def hold(
        self, key: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS
    ) -> AsyncIterator[bool]:
        # A unique value per acquisition, so releasing can check that the lock still
        # belongs to this holder. Deleting by key alone would let a slow holder release
        # the lock a *different* worker has since taken.
        token = str(uuid.uuid4())
        name = f"{KEY_PREFIX}{key}"
        acquired = bool(await self._redis.set(name, token, nx=True, ex=ttl_seconds))
        try:
            yield acquired
        finally:
            if acquired:
                await self._release(name, token)

    async def _release(self, name: str, token: str) -> None:
        try:
            current = await self._redis.get(name)
            if current == token or current == token.encode():
                await self._redis.delete(name)
        except Exception:
            # The lease expires on its own. Failing to release is a delay, not a bug
            # worth failing a completed resync over.
            logger.warning("could not release lock", extra={"lock": name}, exc_info=True)


@dataclass
class MemoryLock:
    """In-process, no expiry. One event loop, so no atomicity concerns."""

    held: set[str] = field(default_factory=set)

    @asynccontextmanager
    async def hold(
        self, key: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS
    ) -> AsyncIterator[bool]:
        if key in self.held:
            yield False
            return
        self.held.add(key)
        try:
            yield True
        finally:
            self.held.discard(key)


__all__ = ["DEFAULT_TTL_SECONDS", "KEY_PREFIX", "Lock", "MemoryLock", "RedisLock"]
