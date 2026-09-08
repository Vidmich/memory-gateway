"""Turning a request-borne identity into a row, on the hot path (SPEC §6.2).

The resolved ``external_id`` is a string; everything downstream needs a ``uuid``. Getting
from one to the other means a database write on first sight, and this module is the whole
of how that is made cheap enough to sit in front of every completion.

**A process cache, because the answer never changes.** ``(organization, external_id)``
maps to one id for the life of that end user, so a hit costs a dictionary lookup and a
busy caller pays nothing at all. The TTL exists only so a purged end user eventually stops
being remembered by a replica that never saw the delete; correctness does not depend on
it, because a stale id resolves to rows that are gone and recall simply finds nothing.

**One statement on a miss, not two.** :meth:`EndUserTransaction.touch` is an upsert with
``RETURNING``, so a first sighting and a thousandth cost the same single round trip and two
simultaneous first sightings produce one row rather than an integrity error.

**Counters are batched, and losing some is fine.** ``request_count`` and ``last_seen_at``
are what the memory browser sorts by; they are not used for billing, quota, or anything a
customer can be wrong about. Writing them per request would turn every completion into a
row update on a hot row — the classic way an innocuous column becomes a lock convoy — so
sightings accumulate in memory and flush on a timer. A process killed mid-window loses a
few counts, which is the correct trade and is stated here so nobody later mistakes the
number for exact.

**Nothing here raises into a request.** A database that is unreachable costs this request
its conversation memory and its end-user attribution; it does not cost the caller their
completion. The gateway's ``on_retrieval_error`` policy is about the *knowledge base* being
unreachable, and turning "PostgreSQL blinked" into a 503 on a fail-closed gateway would be
a much larger outage than the one the setting is asking for.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.background import spawn
from app.core.tenancy import TenantScope
from app.services.end_user import ANONYMOUS, EndUserIdentity
from app.services.end_user_store import EndUserStore

logger = logging.getLogger(__name__)

#: How long a resolved id is trusted without asking again. Five minutes: long enough that
#: a conversation costs one lookup, short enough that a replica forgets an erased end user
#: within a coffee break.
RESOLVER_TTL_SECONDS = 300.0

#: A ceiling on the cache, so a customer generating a fresh anonymous id per request
#: cannot grow it without bound. Ten thousand entries of two uuids and a string is a few
#: megabytes.
RESOLVER_MAX_ENTRIES = 10_000

#: How often sightings are written. Ten seconds keeps "last seen" honest on a screen
#: somebody is watching while keeping a hot end user to six writes a minute.
COUNTER_FLUSH_INTERVAL_SECONDS = 10.0

#: A safety valve: flush early rather than accumulate without bound if the timer is
#: starved. Distinct end users, not requests — a thousand requests from one person is one
#: entry.
COUNTER_MAX_PENDING = 5_000


@dataclass(frozen=True, slots=True)
class ResolvedEndUser:
    """An identity that now has a row."""

    id: uuid.UUID
    external_id: str
    source: str

    @property
    def anonymous(self) -> bool:
        return self.source == ANONYMOUS


@dataclass
class RequestCounters:
    """Sightings, tallied in memory and written in batches.

    Keyed by ``(organization_id, end_user_id)`` because the write is scoped per
    organization — a flush opens one transaction per organization, which is also what
    keeps the update inside :class:`~app.core.tenancy.TenantScope` rather than reaching
    across tenants in a single statement.
    """

    store: EndUserStore
    interval_seconds: float = COUNTER_FLUSH_INTERVAL_SECONDS
    max_pending: int = COUNTER_MAX_PENDING
    _pending: dict[tuple[uuid.UUID, uuid.UUID], int] = field(default_factory=dict)
    _seen_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    _task: asyncio.Task[None] | None = None

    def record(self, organization_id: uuid.UUID, end_user_id: uuid.UUID) -> None:
        """Note one sighting. Synchronous, allocation-free in the common case, and the
        only thing the request path calls."""
        key = (organization_id, end_user_id)
        self._pending[key] = self._pending.get(key, 0) + 1
        self._seen_at = datetime.now(UTC)
        if len(self._pending) >= self.max_pending:
            # Not awaited: the request path must not wait for a write it does not need.
            spawn(self.flush(), name="end-user-counters-overflow")

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="end-user-counters")

    async def stop(self, *, timeout_seconds: float = 5.0) -> None:
        """Stop the loop, then write what is still pending.

        The final flush is not optional for the same reason the request log's is: a
        rolling deploy stops processes constantly, and without it every deploy would drop
        the last window of every replica's sightings.
        """
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout_seconds)
        await self.flush()

    async def flush(self) -> int:
        """Write every pending tally. Returns the number of rows updated."""
        pending, self._pending = self._pending, {}
        if not pending:
            return 0

        seen_at = self._seen_at
        by_organization: dict[uuid.UUID, dict[uuid.UUID, int]] = defaultdict(dict)
        for (organization_id, end_user_id), amount in pending.items():
            by_organization[organization_id][end_user_id] = amount

        written = 0
        for organization_id, counts in by_organization.items():
            try:
                async with self.store.begin(TenantScope.of_organization(organization_id)) as tx:
                    written += await tx.bump(counts, seen_at=seen_at)
                    await tx.commit()
            except Exception:
                # Dropped, not retried. A retry loop in front of an unbounded tally is how
                # a database outage becomes a memory leak, and the thing being lost is a
                # request count.
                logger.warning(
                    "could not flush end-user counters",
                    extra={"organization_id": str(organization_id), "end_users": len(counts)},
                    exc_info=True,
                )
        return written

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            await self.flush()


class EndUserResolver:
    """``EndUserIdentity`` in, ``ResolvedEndUser`` out, usually without touching anything."""

    def __init__(
        self,
        store: EndUserStore,
        *,
        counters: RequestCounters | None = None,
        ttl_seconds: float = RESOLVER_TTL_SECONDS,
        max_entries: int = RESOLVER_MAX_ENTRIES,
    ) -> None:
        self._store = store
        self._counters = counters
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._cache: dict[tuple[uuid.UUID, str], tuple[float, uuid.UUID]] = {}

    async def resolve(
        self, *, organization_id: uuid.UUID, identity: EndUserIdentity | None
    ) -> ResolvedEndUser | None:
        """The row for this identity, creating it on first sight. Never raises."""
        if identity is None:
            return None

        key = (organization_id, identity.external_id)
        end_user_id = self._cached(key)
        if end_user_id is None:
            end_user_id = await self._create(organization_id, identity.external_id)
            if end_user_id is None:
                return None
            self._remember(key, end_user_id)

        if self._counters is not None:
            self._counters.record(organization_id, end_user_id)
        return ResolvedEndUser(
            id=end_user_id, external_id=identity.external_id, source=identity.source
        )

    def forget(self, organization_id: uuid.UUID, external_id: str) -> None:
        """Drop a cached id. Called after an erasure so this process stops attributing
        requests to a row it has just deleted — best effort, because other replicas hold
        their own cache and will let their TTL do it."""
        self._cache.pop((organization_id, external_id), None)

    # -- internals --------------------------------------------------------

    def _cached(self, key: tuple[uuid.UUID, str]) -> uuid.UUID | None:
        found = self._cache.get(key)
        if found is None:
            return None
        stored_at, end_user_id = found
        if time.monotonic() - stored_at >= self._ttl:
            del self._cache[key]
            return None
        return end_user_id

    def _remember(self, key: tuple[uuid.UUID, str], end_user_id: uuid.UUID) -> None:
        if len(self._cache) >= self._max_entries:
            # Oldest first, on insertion order. At this size the difference between that
            # and a true LRU is not measurable, and one of them is two lines.
            del self._cache[next(iter(self._cache))]
        self._cache[key] = (time.monotonic(), end_user_id)

    async def _create(self, organization_id: uuid.UUID, external_id: str) -> uuid.UUID | None:
        try:
            async with self._store.begin(TenantScope.of_organization(organization_id)) as tx:
                row = await tx.touch(external_id)
                await tx.commit()
                return row.id
        except Exception:
            logger.warning(
                "could not resolve an end user; this request has no conversation memory",
                extra={"organization_id": str(organization_id)},
                exc_info=True,
            )
            return None


__all__ = [
    "COUNTER_FLUSH_INTERVAL_SECONDS",
    "COUNTER_MAX_PENDING",
    "RESOLVER_MAX_ENTRIES",
    "RESOLVER_TTL_SECONDS",
    "EndUserResolver",
    "RequestCounters",
    "ResolvedEndUser",
]
