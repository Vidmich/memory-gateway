"""The worker envelope: retry, backoff, dead-lettering, and post-commit enqueue.

None of this needs Redis, and that is the point of splitting the policy out of the queue
adapter. A retry schedule asserted against a live queue is a slow test that fails for
reasons unrelated to the schedule; here it is arithmetic.

The jitter is injected throughout. A backoff asserted against a random draw is a test
that fails one run in twenty and teaches people to re-run rather than to read.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import pytest

from app.core.logging import get_request_id
from app.services.job_queue import MemoryJobQueue, as_payload, deserialize, from_payload, serialize
from app.services.jobs import (
    DeadLetter,
    JobOutbox,
    JobRequest,
    JobRunner,
    MemoryDeadLetters,
    PermanentJobError,
    Retry,
    RetryPolicy,
    delete_key,
    ingest_key,
    resync_key,
)

#: Jitter that always returns the ceiling, so a delay is the schedule rather than a draw.
CEILING = RetryPolicy(jitter=lambda _, ceiling: ceiling)


def request(name: str = "ingest_document", **overrides: Any) -> JobRequest:
    return JobRequest(
        name=name,
        payload={"document_id": "abc"},
        idempotency_key=overrides.pop("idempotency_key", "key-1"),
        **overrides,
    )


# ---------------------------------------------------------------------------
# the policy
# ---------------------------------------------------------------------------


def test_a_transient_failure_is_retried() -> None:
    decision = CEILING.decide(attempt=1, error=RuntimeError("provider hiccup"))

    assert isinstance(decision, Retry)


def test_backoff_doubles_with_each_attempt() -> None:
    policy = RetryPolicy(base_seconds=2.0, jitter=lambda _, ceiling: ceiling)

    assert [policy.delay_for(attempt) for attempt in (1, 2, 3, 4)] == [2.0, 4.0, 8.0, 16.0]


def test_backoff_is_capped() -> None:
    """A queue that waits an hour between attempts has stopped being a queue."""
    policy = RetryPolicy(base_seconds=2.0, cap_seconds=10.0, jitter=lambda _, ceiling: ceiling)

    assert policy.delay_for(20) == 10.0


def test_backoff_is_jittered_by_default() -> None:
    """A provider outage fails every in-flight job at once. A deterministic backoff sends
    them all back at the same instant, and the second wave is as synchronised as the
    first."""
    policy = RetryPolicy(base_seconds=60.0)

    draws = {policy.delay_for(3) for _ in range(50)}

    assert len(draws) > 1
    assert all(0.0 <= draw <= 240.0 for draw in draws)


def test_the_last_attempt_is_dead_lettered_rather_than_retried_forever() -> None:
    policy = RetryPolicy(max_attempts=3, jitter=lambda _, ceiling: ceiling)

    assert isinstance(policy.decide(attempt=2, error=RuntimeError("x")), Retry)
    assert isinstance(policy.decide(attempt=3, error=RuntimeError("x")), DeadLetter)


def test_a_permanent_failure_is_dead_lettered_on_the_first_attempt() -> None:
    """A payload naming a row that does not exist will not start existing. Retrying it
    four times spends four workers to reach the same answer."""
    decision = CEILING.decide(attempt=1, error=PermanentJobError("document is gone"))

    assert isinstance(decision, DeadLetter)
    assert decision.reason == "document is gone"


def test_the_dead_letter_reason_names_the_attempt_count() -> None:
    policy = RetryPolicy(max_attempts=2, jitter=lambda _, ceiling: ceiling)

    decision = policy.decide(attempt=2, error=RuntimeError("connection reset"))

    assert isinstance(decision, DeadLetter)
    assert "2 attempts" in decision.reason
    assert "connection reset" in decision.reason


# ---------------------------------------------------------------------------
# the runner
# ---------------------------------------------------------------------------


def runner(
    handler: Any, *, policy: RetryPolicy | None = None
) -> tuple[JobRunner, MemoryJobQueue, MemoryDeadLetters]:
    queue = MemoryJobQueue()
    letters = MemoryDeadLetters()
    return (
        JobRunner(
            {"ingest_document": handler},
            queue=queue,
            dead_letters=letters,
            policy=policy or CEILING,
        ),
        queue,
        letters,
    )


async def test_a_successful_job_enqueues_nothing() -> None:
    seen: list[Mapping[str, Any]] = []

    async def handler(payload: Mapping[str, Any]) -> None:
        seen.append(payload)

    job_runner, queue, letters = runner(handler)

    assert await job_runner.run(request()) is None
    assert seen == [{"document_id": "abc"}]
    assert queue.pending == []
    assert letters.records == []


async def test_a_failing_job_is_re_enqueued_with_the_next_attempt_number() -> None:
    async def handler(_: Mapping[str, Any]) -> None:
        raise RuntimeError("provider down")

    job_runner, queue, _ = runner(handler)

    await job_runner.run(request())

    assert len(queue.pending) == 1
    assert queue.pending[0].attempt == 2
    assert queue.pending[0].delay_seconds > 0


async def test_a_retry_does_not_deduplicate_against_the_attempt_that_failed() -> None:
    """The original key may still be reserved by the queue. An un-qualified retry would
    be silently swallowed, and the document would sit in `extracting` forever."""

    async def handler(_: Mapping[str, Any]) -> None:
        raise RuntimeError("nope")

    job_runner, queue, _ = runner(handler)

    await job_runner.run(request(idempotency_key="ingest:doc:hash"))

    assert queue.pending[0].idempotency_key == "ingest:doc:hash:retry1"


async def test_an_exhausted_job_is_written_to_the_dead_letters() -> None:
    async def handler(_: Mapping[str, Any]) -> None:
        raise RuntimeError("still down")

    job_runner, queue, letters = runner(handler, policy=RetryPolicy(max_attempts=1))

    await job_runner.run(request())

    assert queue.pending == []
    [(recorded, reason)] = letters.records
    assert recorded.payload == {"document_id": "abc"}
    assert "still down" in reason


async def test_a_job_this_build_does_not_know_is_dead_lettered() -> None:
    """A payload for a job that a rollback removed. Retrying cannot help, and dropping it
    silently would lose the evidence of a bad deploy."""

    async def handler(_: Mapping[str, Any]) -> None:
        return None

    job_runner, queue, letters = runner(handler)

    decision = await job_runner.run(request(name="from_the_future"))

    assert isinstance(decision, DeadLetter)
    assert queue.pending == []
    assert len(letters.records) == 1


async def test_a_job_retries_until_it_succeeds() -> None:
    attempts: list[int] = []

    async def handler(_: Mapping[str, Any]) -> None:
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise RuntimeError("not yet")

    job_runner, queue, letters = runner(handler)

    await queue.enqueue(request())
    await queue.drain(job_runner)

    assert attempts == [1, 2, 3]
    assert letters.records == []


async def test_a_dead_letter_sink_that_fails_does_not_break_the_worker() -> None:
    """The structured log line is the durable-enough record when the database is the
    thing that is broken."""

    class Broken:
        async def record(self, request: JobRequest, *, reason: str) -> None:
            raise RuntimeError("database is down too")

    queue = MemoryJobQueue()

    async def handler(_: Mapping[str, Any]) -> None:
        raise RuntimeError("original failure")

    job_runner = JobRunner(
        {"ingest_document": handler},
        queue=queue,
        dead_letters=Broken(),
        policy=RetryPolicy(max_attempts=1),
    )

    assert isinstance(await job_runner.run(request()), DeadLetter)


# ---------------------------------------------------------------------------
# the outbox
# ---------------------------------------------------------------------------


async def test_the_outbox_holds_jobs_until_it_is_flushed() -> None:
    queue = MemoryJobQueue()
    outbox = JobOutbox(queue)

    outbox.add("ingest_document", {"a": 1}, idempotency_key="k")

    assert queue.submitted == []

    assert await outbox.flush() == 1
    assert queue.names() == ["ingest_document"]


async def test_a_discarded_outbox_enqueues_nothing() -> None:
    """What a rolled-back transaction does. A job naming a row that was never written is
    a worker failure nobody can explain from the evidence."""
    queue = MemoryJobQueue()
    outbox = JobOutbox(queue)
    outbox.add("ingest_document", {"a": 1}, idempotency_key="k")

    outbox.discard()
    await outbox.flush()

    assert queue.submitted == []


async def test_a_queue_that_is_down_does_not_undo_committed_work() -> None:
    """The bytes are stored and the row exists. Raising here would return a 500 for an
    upload that actually succeeded; a resync recovers the enqueue."""

    class Broken(MemoryJobQueue):
        async def enqueue(self, request: JobRequest) -> str | None:
            raise ConnectionError("redis is away")

    outbox = JobOutbox(Broken())
    outbox.add("ingest_document", {"a": 1}, idempotency_key="k")

    assert await outbox.flush() == 0


async def test_the_outbox_carries_the_current_request_id() -> None:
    """Joins a worker log line, minutes later on another process, to the API call that
    caused it."""
    from app.core.logging import bind_request_id

    queue = MemoryJobQueue()
    outbox = JobOutbox(queue)

    with bind_request_id("req-42"):
        outbox.add("ingest_document", {"a": 1}, idempotency_key="k")

    assert get_request_id() is None
    await outbox.flush()
    assert queue.submitted[0].request_id == "req-42"


# ---------------------------------------------------------------------------
# the queue
# ---------------------------------------------------------------------------


async def test_an_identical_pending_job_is_not_enqueued_twice() -> None:
    queue = MemoryJobQueue()
    job = request()

    assert await queue.enqueue(job) is not None
    assert await queue.enqueue(job) is None
    assert len(queue.pending) == 1


async def test_the_depth_is_what_is_waiting() -> None:
    queue = MemoryJobQueue()
    await queue.enqueue(request(idempotency_key="a"))
    await queue.enqueue(request(idempotency_key="b"))

    assert await queue.depth() == 2


async def test_a_queue_that_cannot_be_reached_fails_its_ping() -> None:
    queue = MemoryJobQueue(reachable=False)

    with pytest.raises(ConnectionError):
        await queue.ping()


# ---------------------------------------------------------------------------
# the wire format
# ---------------------------------------------------------------------------


def test_a_job_round_trips_through_the_wire_format() -> None:
    original = JobRequest(
        name="ingest_document",
        payload={"organization_id": "o", "document_id": "d"},
        idempotency_key="k",
        request_id="r",
        attempt=3,
    )

    restored = from_payload(deserialize(serialize(as_payload(original))))

    assert restored.name == original.name
    assert restored.payload == original.payload
    assert restored.idempotency_key == original.idempotency_key
    assert restored.request_id == original.request_id
    assert restored.attempt == original.attempt


def test_the_wire_format_is_json_not_pickle() -> None:
    """Anything able to write to Redis can otherwise execute code in a worker. The
    payload is three strings; JSON costs nothing and removes the class of problem."""
    raw = serialize(as_payload(request()))

    assert raw.startswith(b"{")
    assert b"ingest_document" in raw


def test_a_payload_from_an_older_build_still_loads() -> None:
    """Missing keys take their defaults rather than raising. A rolling deploy has jobs of
    both shapes in the queue at once."""
    restored = from_payload({"name": "ingest_document", "payload": {"document_id": "d"}})

    assert restored.attempt == 1
    assert restored.request_id is None


# ---------------------------------------------------------------------------
# idempotency keys
# ---------------------------------------------------------------------------


def test_the_ingest_key_covers_the_document_and_its_content() -> None:
    """Keying on the document alone would make a re-upload of a corrected file a
    duplicate of the ingestion still running for the old one — and the fix would silently
    never be indexed."""
    document = uuid.uuid4()

    assert ingest_key(document, "hash-a") != ingest_key(document, "hash-b")
    assert ingest_key(document, "hash-a") == ingest_key(document, "hash-a")


def test_an_unknown_content_hash_still_produces_a_stable_key() -> None:
    document = uuid.uuid4()

    assert ingest_key(document, None) == ingest_key(document, None)


def test_the_connector_keys_are_one_per_connector() -> None:
    connector = uuid.uuid4()

    assert resync_key(connector) != delete_key(connector)
    assert resync_key(connector) == resync_key(connector)
