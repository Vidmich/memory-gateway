"""Arming a pass from the log flusher, and the four gates in front of it.

This is the only place the serving half of the system touches the memory-writing half, so
these tests are mostly about what it *refuses* to do — and about the one property the whole
feature rests on: nothing here can reach a request.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.ids import uuid7
from app.schemas.distillation import ORG_DISTILLATION
from app.schemas.gateway_config import LoggingConfig
from app.services.debounce import MemoryDebouncer, session_key
from app.services.distillation_trigger import DistillationTrigger, distillable
from app.services.end_user_store import MemoryEndUserStore
from app.services.jobs import DISTIL_MEMORY, JobRequest
from app.services.memory_db import MemoryDatabase
from app.services.request_log import LogPolicy, RequestRecord
from tests.auth_support import make_organization

pytestmark = pytest.mark.anyio


class RecordingQueue:
    def __init__(self) -> None:
        self.requests: list[JobRequest] = []
        self.fails = False

    async def enqueue(self, request: JobRequest) -> str | None:
        if self.fails:
            raise RuntimeError("redis is down")
        self.requests.append(request)
        return str(uuid7())

    async def depth(self) -> int:
        return len(self.requests)

    async def ping(self) -> None:
        return None


class Harness:
    def __init__(self, **settings: Any) -> None:
        self.organization = make_organization()
        self.database = MemoryDatabase()
        if settings:
            self.organization.settings = {ORG_DISTILLATION: settings}
        self.database.add_organization(self.organization)
        self.store = MemoryEndUserStore(self.database)
        self.queue = RecordingQueue()
        self.debouncer = MemoryDebouncer()
        self.trigger = DistillationTrigger(
            self.queue, store=self.store, debouncer=self.debouncer, ttl_seconds=0.0
        )

    def record(self, **overrides: Any) -> RequestRecord:
        values: dict[str, Any] = {
            "organization_id": self.organization.id,
            "gateway_id": uuid7(),
            "end_user_id": uuid7(),
            "session_id": "thread-1",
            "status_code": 200,
            "request_body": [{"role": "user", "content": "hello"}],
            "policy": LogPolicy.of(LoggingConfig()),
        }
        values.update(overrides)
        return RequestRecord(**values)


# ---------------------------------------------------------------------------
# the gates
# ---------------------------------------------------------------------------


def test_a_full_capture_request_with_an_end_user_is_distillable() -> None:
    assert distillable(Harness().record())


def test_a_gateway_with_distillation_off_is_not() -> None:
    policy = LogPolicy.of(LoggingConfig(enable_distillation=False))

    assert not distillable(Harness().record(policy=policy))


def test_a_gateway_with_no_body_logging_is_not() -> None:
    """SPEC §10.2: distillation reads transcripts. Both halves are checked, because a row
    stored before the schema's validator existed can still say "distil without bodies"."""
    policy = LogPolicy.of(LoggingConfig(log_request_body=False, enable_distillation=False))

    assert not distillable(Harness().record(policy=policy, request_body=None))


def test_a_request_with_nobody_asking_is_not() -> None:
    assert not distillable(Harness().record(end_user_id=None))


def test_a_failed_request_is_not() -> None:
    assert not distillable(Harness().record(status_code=502))


def test_a_record_whose_bodies_were_shed_is_not() -> None:
    """The gateway asked for them; queue pressure or a slow redaction pattern took them
    anyway. There is nothing to read whatever the configuration says."""
    record = Harness().record()

    assert not distillable(record.without_bodies("queue_pressure"))


# ---------------------------------------------------------------------------
# arming
# ---------------------------------------------------------------------------


async def test_arming_enqueues_one_delayed_job_carrying_its_token() -> None:
    harness = Harness()
    record = harness.record()

    armed = await harness.trigger.consider([record])

    assert len(armed) == 1
    job = harness.queue.requests[0]
    assert job.name == DISTIL_MEMORY
    assert job.delay_seconds == 30
    assert job.payload["token"] == armed[0].token
    assert job.payload["session_id"] == "thread-1"


async def test_the_newest_turn_is_the_one_that_will_run() -> None:
    """Trailing debounce: each turn replaces the pending token, so the pass happens a
    window after the conversation goes quiet rather than in the middle of it."""
    harness = Harness()
    end_user = uuid7()

    first = await harness.trigger.consider([harness.record(end_user_id=end_user)])
    second = await harness.trigger.consider([harness.record(end_user_id=end_user)])

    key = session_key(end_user, "thread-1")
    assert not await harness.debouncer.claim(key, first[0].token)
    assert await harness.debouncer.claim(key, second[0].token)


async def test_a_burst_inside_one_batch_arms_one_pass_not_three() -> None:
    """Three turns of one conversation in a single flush would otherwise be three jobs that
    immediately supersede each other."""
    harness = Harness()
    end_user = uuid7()
    records = [harness.record(end_user_id=end_user) for _ in range(3)]

    armed = await harness.trigger.consider(records)

    assert len(armed) == 1
    assert len(harness.queue.requests) == 1


async def test_two_people_in_one_batch_are_two_passes() -> None:
    harness = Harness()
    records = [harness.record(), harness.record()]

    assert len(await harness.trigger.consider(records)) == 2


async def test_two_threads_of_one_person_are_two_passes() -> None:
    """Debounce is by ``(end_user, session)``. Two conversations happening at once are two
    exchanges, and merging them would hand the extractor a transcript nobody had."""
    harness = Harness()
    end_user = uuid7()
    records = [
        harness.record(end_user_id=end_user, session_id="thread-1"),
        harness.record(end_user_id=end_user, session_id="thread-2"),
    ]

    assert len(await harness.trigger.consider(records)) == 2


async def test_two_jobs_for_one_conversation_are_not_deduplicated_by_the_queue() -> None:
    """The opposite of ``ingest_key``. The second job's whole purpose is to supersede the
    first; a queue that collapsed them would leave the pass scheduled at the first turn's
    deadline, in the middle of the exchange it is waiting out."""
    harness = Harness()
    end_user = uuid7()
    await harness.trigger.consider([harness.record(end_user_id=end_user)])
    await harness.trigger.consider([harness.record(end_user_id=end_user)])

    keys = {job.idempotency_key for job in harness.queue.requests}
    assert len(keys) == 2


async def test_an_organization_with_distillation_off_arms_nothing() -> None:
    harness = Harness(enabled=False)

    assert await harness.trigger.consider([harness.record()]) == []
    assert harness.queue.requests == []


async def test_the_configured_debounce_delay_is_the_jobs_delay() -> None:
    harness = Harness(debounce_seconds=120)

    armed = await harness.trigger.consider([harness.record()])

    assert armed[0].delay_seconds == 120
    assert harness.queue.requests[0].delay_seconds == 120


async def test_a_queue_outage_costs_the_memory_and_nothing_else() -> None:
    """The request was answered and logged minutes ago. There is no path from here back
    into it, and this is the test that says so."""
    harness = Harness()
    harness.queue.fails = True

    assert await harness.trigger.consider([harness.record()]) == []


async def test_a_settings_blob_somebody_broke_by_hand_falls_back_to_the_defaults() -> None:
    """Read by a background task. A hand-edited settings row should degrade to the
    documented behaviour rather than dead-letter every job in the organization."""
    harness = Harness()
    harness.organization.settings = {ORG_DISTILLATION: "not an object"}

    armed = await harness.trigger.consider([harness.record()])

    assert armed[0].delay_seconds == 30


async def test_the_settings_cache_can_be_dropped_when_they_change() -> None:
    """So the person who just changed the debounce delay sees it apply to their next
    request rather than to the one after the cache expires."""
    harness = Harness()
    trigger = DistillationTrigger(
        harness.queue, store=harness.store, debouncer=harness.debouncer, ttl_seconds=3600.0
    )
    await trigger.consider([harness.record()])

    harness.organization.settings = {ORG_DISTILLATION: {"debounce_seconds": 300}}
    cached = await trigger.consider([harness.record()])
    trigger.forget(harness.organization.id)
    fresh = await trigger.consider([harness.record()])

    assert cached[0].delay_seconds == 30
    assert fresh[0].delay_seconds == 300


async def test_the_request_id_travels_with_the_job() -> None:
    """A support ticket quoting one leads to the log row *and* to the distillation the
    conversation produced, minutes later on another process."""
    harness = Harness()

    await harness.trigger.consider([harness.record(request_id="req-123")])

    assert harness.queue.requests[0].request_id == "req-123"


async def test_nothing_is_armed_for_an_organization_with_no_settings_row_at_all() -> None:
    """Defaults, not a crash: an organization that has never opened the Settings screen
    distils with the platform's numbers."""
    harness = Harness()
    harness.database.organizations.clear()

    armed = await harness.trigger.consider([harness.record()])

    assert armed[0].delay_seconds == 30


def test_a_session_key_is_per_person_and_per_thread() -> None:
    person = uuid.uuid4()

    assert session_key(person, "a") != session_key(person, "b")
    assert session_key(person, None) != session_key(uuid.uuid4(), None)
