"""Deciding that a conversation is worth distilling, and arming a pass (SPEC §6.4, step 1).

This is the only place where the serving half of the system touches the memory-writing
half, so it is written to be unable to hurt it. It runs inside
:class:`~app.services.request_log.LogFlusher` — a background task that already swallows its
own failures — *after* the transcripts have been committed, and everything it does is
wrapped: a Redis outage, a queue outage or a settings blob somebody broke by hand costs the
memory that would have been written and nothing else. There is no path from here back into
a request.

**Why after the write and not during it.** A job enqueued before its transcript is committed
is a job that reads nothing and marks nothing, on a worker that then has no way to know it
was early. The transaction that justified the work has to have landed first — the same rule
:class:`~app.services.jobs.JobOutbox` states for ingestion, applied one layer down.

**Four gates, and they answer four different support questions.** A gateway with body
logging off cannot distil, because there is nothing to read; a gateway with
``enable_distillation`` off has been told not to; a request with no end user has nobody to
remember; a failed request has no conversation, only an outage. Each of those is a different
sentence on a screen, so they are four checks rather than one.

**The organization's settings are cached, briefly.** The flusher sees every request in the
process, and reading a settings row per record would put a database round trip inside the
one component that must never become slow. Sixty seconds of staleness means a debounce delay
somebody just changed takes a minute to apply, which is the correct thing to be relaxed
about.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.tenancy import TenantScope
from app.schemas.distillation import DistillationConfig, organization_distillation
from app.services.debounce import Debouncer, session_key
from app.services.end_user_store import EndUserStore
from app.services.jobs import DISTIL_MEMORY, JobQueue, JobRequest, distil_key
from app.services.request_log import RequestRecord

logger = logging.getLogger(__name__)

#: How long an organization's distillation settings are held. Short enough that a change is
#: not a mystery, long enough that a busy process reads the row once a minute rather than
#: once a request.
SETTINGS_TTL_SECONDS = 60.0

#: Organizations whose settings are cached at once. A process serving more tenants than this
#: simply reads more often, which is the right degradation.
SETTINGS_MAX_ENTRIES = 1000


@dataclass(frozen=True, slots=True)
class Armed:
    """One conversation with a pass pending. Returned so a test can assert on the plan
    rather than on the queue's internals."""

    organization_id: uuid.UUID
    end_user_id: uuid.UUID
    session_id: str | None
    token: str
    delay_seconds: int


def distillable(record: RequestRecord) -> bool:
    """Whether one logged request could feed conversation memory.

    Pure, and separate from the enqueue, because this is the part with four independent
    reasons and the part a test wants to call directly.
    """
    if not record.policy.distillation:
        return False
    if record.end_user_id is None:
        return False
    if record.status_code >= 400:
        # A 502 has no answer and usually a question that was never really asked. Distilling
        # one teaches the assistant about an outage.
        return False
    # After redaction: a record whose bodies were dropped for budget or queue pressure has
    # nothing to read, whatever the gateway asked for.
    return record.request_body is not None


class DistillationTrigger:
    def __init__(
        self,
        queue: JobQueue,
        *,
        store: EndUserStore,
        debouncer: Debouncer,
        ttl_seconds: float = SETTINGS_TTL_SECONDS,
        max_entries: int = SETTINGS_MAX_ENTRIES,
    ) -> None:
        self._queue = queue
        self._store = store
        self._debouncer = debouncer
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._cache: dict[uuid.UUID, tuple[float, DistillationConfig]] = {}

    async def consider(self, records: Sequence[RequestRecord]) -> list[Armed]:
        """Arm a pass for every conversation in this batch that wants one.

        Grouped by ``(end_user, session)`` first: a batch holding three turns of the same
        conversation is one pending pass, not three that immediately supersede each other.
        The newest record in the group supplies the organization, because they all share it.
        """
        groups: dict[tuple[uuid.UUID, uuid.UUID, str | None], RequestRecord] = {}
        for record in records:
            if not distillable(record) or record.end_user_id is None:
                continue
            groups[(record.organization_id, record.end_user_id, record.session_id)] = record

        armed: list[Armed] = []
        for (organization_id, end_user_id, session), record in groups.items():
            try:
                plan = await self._arm(organization_id, end_user_id, session, record)
            except Exception:
                # The memory this conversation would have produced is lost, and that is the
                # whole of the damage. The request was answered and logged minutes ago.
                logger.warning(
                    "could not arm a distillation pass",
                    extra={
                        "organization_id": str(organization_id),
                        "end_user_id": str(end_user_id),
                    },
                    exc_info=True,
                )
                continue
            if plan is not None:
                armed.append(plan)
        return armed

    async def _arm(
        self,
        organization_id: uuid.UUID,
        end_user_id: uuid.UUID,
        session: str | None,
        record: RequestRecord,
    ) -> Armed | None:
        config = await self._config(organization_id)
        if not config.enabled:
            return None

        key = session_key(end_user_id, session)
        token = await self._debouncer.arm(key, ttl_seconds=config.debounce_seconds)
        await self._queue.enqueue(
            JobRequest(
                name=DISTIL_MEMORY,
                payload={
                    "organization_id": str(organization_id),
                    "end_user_id": str(end_user_id),
                    "session_id": session,
                    "token": token,
                },
                # The token is in the key, so two turns of one conversation are two
                # different jobs. They have to be: the second one's whole purpose is to
                # supersede the first, and a queue that deduplicated them would leave the
                # pass to run at the *first* turn's deadline.
                idempotency_key=distil_key(end_user_id, session, token),
                request_id=record.request_id,
                delay_seconds=float(config.debounce_seconds),
            )
        )
        return Armed(
            organization_id=organization_id,
            end_user_id=end_user_id,
            session_id=session,
            token=token,
            delay_seconds=config.debounce_seconds,
        )

    async def _config(self, organization_id: uuid.UUID) -> DistillationConfig:
        now = time.monotonic()
        cached = self._cache.get(organization_id)
        if cached is not None and cached[0] > now:
            return cached[1]

        async with self._store.begin(TenantScope.of_organization(organization_id)) as transaction:
            config = organization_distillation(
                await transaction.organization_settings(organization_id)
            )
        if len(self._cache) >= self._max_entries:
            # Not an LRU. The population is organizations with live traffic on one process,
            # the entries cost a hundred bytes, and a full clear once in a while is cheaper
            # to reason about than an eviction order nobody will ever tune.
            self._cache.clear()
        self._cache[organization_id] = (now + self._ttl, config)
        return config

    def forget(self, organization_id: uuid.UUID) -> None:
        """Drop a cached setting. Called when the organization's distillation settings are
        saved, so the screen and the behaviour agree immediately for the person who just
        pressed the button."""
        self._cache.pop(organization_id, None)


__all__ = [
    "SETTINGS_MAX_ENTRIES",
    "SETTINGS_TTL_SECONDS",
    "Armed",
    "DistillationTrigger",
    "distillable",
]
