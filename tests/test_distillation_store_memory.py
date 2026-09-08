"""The distillation store contract, against the in-memory twin.

The same rows PostgreSQL gets in ``tests/test_distillation_db.py``, seeded here as objects.
Each check gets a fresh fixture, because several of them write runs and one marks
transcripts distilled — sharing would make the order they run in load-bearing.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.ids import uuid7
from app.db.models import EndUser, MemoryFact, Organization, RequestLog, Transcript
from app.services.distillation_store import MemoryDistillationStore
from app.services.memory_db import MemoryDatabase
from tests.distillation_store_contract import CHECKS, NOW, Check, Fixture

pytestmark = pytest.mark.anyio

#: ``(session_id, distilled, status_code, minutes_ago)`` for acme's traffic. One thread
#: with two undistilled turns and one already covered, one thread with no session id at
#: all, and one that errored.
ACME_TRAFFIC: tuple[tuple[str | None, bool, int, int], ...] = (
    ("thread-1", False, 200, 30),
    ("thread-1", False, 200, 20),
    ("thread-1", True, 200, 40),
    (None, False, 200, 25),
    ("thread-errors", False, 502, 15),
)


def _organization(name: str, slug: str) -> Organization:
    return Organization(id=uuid7(), name=name, slug=slug, status="active", settings={})


def _end_user(organization: Organization, external_id: str) -> EndUser:
    return EndUser(
        id=uuid7(),
        organization_id=organization.id,
        external_id=external_id,
        first_seen_at=NOW,
        last_seen_at=NOW,
        request_count=1,
    )


def build_fixture() -> tuple[MemoryDatabase, Fixture]:
    """The rows both halves of the contract are run against."""
    from datetime import timedelta

    database = MemoryDatabase()
    acme = _organization("Acme", "acme")
    globex = _organization("Globex", "globex")
    database.add_organization(acme)
    database.add_organization(globex)

    acme_end_user = _end_user(acme, "alice")
    globex_end_user = _end_user(globex, "alice")
    database.add_end_user(acme_end_user)
    database.add_end_user(globex_end_user)

    # One live fact, so the health check has something to average.
    database.add_fact(
        MemoryFact(
            id=uuid7(),
            organization_id=acme.id,
            end_user_id=acme_end_user.id,
            text="Works in Rust.",
            kind="fact",
            confidence=1.0,
            created_at=NOW,
            last_seen_at=NOW,
        )
    )

    logs: list[tuple[uuid.UUID, str | None, bool]] = []
    for session, distilled, status, minutes in ACME_TRAFFIC:
        when = NOW - timedelta(minutes=minutes)
        log = RequestLog(
            id=uuid7(),
            created_at=when,
            organization_id=acme.id,
            gateway_id=uuid7(),
            end_user_id=acme_end_user.id,
            session_id=session,
            status_code=status,
            latency_total_ms=100,
            retrieved_chunk_ids=[],
            retrieved_fact_ids=[],
            failover_attempts=[],
        )
        database.request_logs[log.id] = log
        database.transcripts[log.id] = Transcript(
            request_log_id=log.id,
            created_at=when,
            organization_id=acme.id,
            request_body=[{"role": "user", "content": "I work in Rust."}],
            assembled_prompt=None,
            response_body="Noted.",
            distilled_at=when if distilled else None,
        )
        logs.append((log.id, session, distilled))

    # Globex's own traffic, so a scope failure has something to leak.
    other = RequestLog(
        id=uuid7(),
        created_at=NOW,
        organization_id=globex.id,
        gateway_id=uuid7(),
        end_user_id=globex_end_user.id,
        session_id="thread-1",
        status_code=200,
        latency_total_ms=100,
        retrieved_chunk_ids=[],
        retrieved_fact_ids=[],
        failover_attempts=[],
    )
    database.request_logs[other.id] = other
    database.transcripts[other.id] = Transcript(
        request_log_id=other.id,
        created_at=NOW,
        organization_id=globex.id,
        request_body=[{"role": "user", "content": "secret"}],
        assembled_prompt=None,
        response_body="ok",
        distilled_at=None,
    )

    return database, Fixture(
        store=MemoryDistillationStore(database),
        acme=acme,
        globex=globex,
        acme_end_user=acme_end_user,
        globex_end_user=globex_end_user,
        acme_logs=tuple(logs),
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_the_memory_store_satisfies_the_contract(check: Check) -> None:
    _, fixture = build_fixture()
    await check(fixture)
