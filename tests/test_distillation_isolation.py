"""Distillation cannot affect the serving path, and the backfill is safe to run twice.

Two acceptance criteria, and they are the two that make the rest of task 13 safe to ship.

**"Breaking the distillation model entirely leaves completions unaffected."** The property
is structural — a pass runs on a worker, minutes after the response was returned — so what
is left to test is the one place the two halves touch: the log flusher's call to the
trigger. These break it the way a Redis or queue outage would, and assert that a request is
still answered and still logged.

**"A backfill over a date range is idempotent."** ``transcripts.distilled_at`` is what makes
that true, and it is set *after* the facts are written. Running the same range twice
produces the facts once.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.ids import uuid7
from app.schemas.gateway_config import LoggingConfig
from app.services.memory_db import MemoryDatabase
from app.services.request_log import LogPolicy, RequestRecord
from tests.conftest import build_harness_parts, build_memory, build_proxy_app
from tests.distillation_support import ScriptedModel, build_distillation, facts_json
from tests.monitoring_support import build_logs
from tests.support import MockUpstream, completion

pytestmark = pytest.mark.anyio


class BrokenSubscriber:
    """A trigger that fails the way a Redis or queue outage would."""

    def __init__(self) -> None:
        self.calls = 0

    async def consider(self, records: Any) -> Any:
        self.calls += 1
        raise RuntimeError("redis is down")


class Watcher:
    """Records each batch it is told about, and how much had been written when it was."""

    def __init__(self) -> None:
        self.database: MemoryDatabase | None = None
        self.batches: list[list[RequestRecord]] = []
        self.transcripts_when_called: list[int] = []

    async def consider(self, records: Any) -> Any:
        assert self.database is not None
        self.batches.append(list(records))
        self.transcripts_when_called.append(len(self.database.transcripts))
        return []


def record() -> RequestRecord:
    return RequestRecord(
        organization_id=uuid7(),
        gateway_id=uuid7(),
        end_user_id=uuid7(),
        session_id="thread-1",
        status_code=200,
        request_body=[{"role": "user", "content": "hello"}],
        policy=LogPolicy.of(LoggingConfig()),
    )


# ---------------------------------------------------------------------------
# the flusher
# ---------------------------------------------------------------------------


async def test_the_subscriber_is_told_only_after_the_transcripts_are_committed() -> None:
    """A job enqueued before its transcript exists is a job that reads nothing and marks
    nothing, on a worker with no way to know it was early."""
    watcher = Watcher()
    logs = build_logs(subscriber=watcher)
    watcher.database = logs.database
    logs.queue.submit(record())

    await logs.flush()

    assert watcher.transcripts_when_called == [1]


async def test_a_broken_trigger_does_not_lose_the_log_batch() -> None:
    """Failing here costs the conversation memory those requests would have produced.
    Failing *upward* would lose a batch of logs that has already been written, which is a
    strictly worse trade for a strictly less important feature."""
    broken = BrokenSubscriber()
    logs = build_logs(subscriber=broken)
    logs.queue.submit(record())

    written = await logs.flush()

    assert written == 1
    assert broken.calls == 1
    assert len(logs.database.request_logs) == 1


async def test_a_flusher_with_no_subscriber_at_all_still_writes() -> None:
    """What a worker process builds. Conversation memory is optional wiring, not a
    dependency of logging."""
    logs = build_logs()
    logs.queue.submit(record())

    assert await logs.flush() == 1
    assert len(logs.database.transcripts) == 1


# ---------------------------------------------------------------------------
# the serving path
# ---------------------------------------------------------------------------


async def test_a_completion_succeeds_while_the_trigger_is_broken(
    upstream: MockUpstream,
) -> None:
    """The acceptance criterion, from the client's side: break the half of distillation
    that lives in the API process, and a request is still answered and still logged."""
    upstream.behaviour = replace(upstream.behaviour, body=completion("Understood."))
    resolver, authenticator, token = build_harness_parts(upstream)
    broken = BrokenSubscriber()
    logs = build_logs(subscriber=broken)
    application = build_proxy_app(resolver, authenticator, logs, build_memory())

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                f"/g/{resolver.gateway.slug}/v1/chat/completions",
                headers={"Authorization": f"Bearer {token}", "X-Gateway-User": "alice"},
                json={
                    "model": resolver.gateway.virtual_model,
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
            await logs.flush()

    assert response.status_code == 200, response.text
    assert broken.calls == 1
    assert len(logs.rows) == 1
    assert logs.transcript(logs.rows[0].id) is not None


# ---------------------------------------------------------------------------
# the backfill
# ---------------------------------------------------------------------------


async def test_a_backfill_over_a_range_is_idempotent() -> None:
    """``distilled_at`` is what makes it so, and it is set after the facts are written —
    so a crash in between costs one repeated pass, which dedupe absorbs, rather than one
    silently skipped conversation."""
    fixture = build_distillation(model=ScriptedModel(facts_json({"text": "Works in Rust."})))
    alice = await fixture.end_user()
    fixture.log(alice, created_at=datetime.now(UTC) - timedelta(days=2))

    first = await backfill(fixture)
    second = await backfill(fixture)

    assert first == 1
    assert second == 0
    assert await fixture.texts_of(alice) == ["Works in Rust."]


async def test_a_backfill_covers_every_thread_in_the_window() -> None:
    fixture = build_distillation()
    alice = await fixture.end_user("alice")
    bob = await fixture.end_user("bob")
    fixture.log(alice, session_id="thread-1")
    fixture.log(bob, session_id="thread-2")

    assert await backfill(fixture) == 2


async def test_a_backfill_leaves_a_window_it_was_not_asked_about_alone() -> None:
    fixture = build_distillation()
    alice = await fixture.end_user()
    fixture.log(alice, created_at=datetime.now(UTC) - timedelta(days=90))

    assert await backfill(fixture) == 0
    assert await fixture.texts_of(alice) == []


async def test_a_backfill_honours_the_daily_cap() -> None:
    """A backfill that bypassed the cost guard would be the one way to spend a month's
    budget in an afternoon."""
    fixture = build_distillation(daily_call_cap=1)
    alice = await fixture.end_user("alice")
    bob = await fixture.end_user("bob")
    fixture.log(alice, session_id="thread-1")
    fixture.log(bob, session_id="thread-2")

    await backfill(fixture)

    assert fixture.model.calls == 1


async def backfill(fixture: Any, *, days: int = 30) -> int:
    """What ``python -m app.cli distil-backfill`` does, without the process wiring.

    The same two calls in the same order — list the pending threads, run the real pass over
    each — so what these tests assert is what the command does.
    """
    end = datetime.now(UTC) + timedelta(seconds=1)
    async with fixture.store.begin(fixture.scope) as transaction:
        sessions = list(
            await transaction.pending_sessions(start=end - timedelta(days=days), end=end, limit=200)
        )
    for session in sessions:
        await fixture.distiller.run(
            organization_id=session.organization_id,
            end_user_id=session.end_user_id,
            session_id=session.session_id,
        )
    return len(sessions)
