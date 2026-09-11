"""One set of assertions for both validation stores (task 103): the audit store and the
evaluation store, against PostgreSQL and against the memory twin.

Scoping first, as everywhere: an organization reads its own audits, sets, items and runs
and nobody else's, whatever id it asks for. Then the reads the screens depend on: the
latest audit per kind, the latest per connector across the organization, item counts split
the way the headline needs them, and runs newest first.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.tenancy import TenantScope
from app.db.models import Connector, Gateway, Organization
from app.services.evaluation_store import EvaluationStore
from app.services.index_audit_store import IndexAuditStore
from tests.validation_support import (
    make_evaluation_item,
    make_evaluation_run,
    make_evaluation_set,
    make_index_audit,
)


@dataclass(frozen=True)
class Fixture:
    audits: IndexAuditStore
    evaluations: EvaluationStore
    acme: Organization
    globex: Organization
    acme_connector: Connector
    globex_connector: Connector
    acme_gateway: Gateway
    globex_gateway: Gateway

    def scope(self, organization: Organization) -> TenantScope:
        return TenantScope(role="org_admin", organization_id=organization.id)

    @property
    def acme_scope(self) -> TenantScope:
        return self.scope(self.acme)

    @property
    def globex_scope(self) -> TenantScope:
        return self.scope(self.globex)


Check = Callable[[Fixture], Awaitable[None]]
CHECKS: list[Check] = []


def check(function: Check) -> Check:
    CHECKS.append(function)
    return function


NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# audits
# ---------------------------------------------------------------------------


@check
async def the_latest_audit_per_kind_is_the_newest_row_whatever_its_status(
    fixture: Fixture,
) -> None:
    connector = fixture.acme_connector
    older = make_index_audit(connector.id, fixture.acme.id, created_at=NOW - timedelta(hours=2))
    newer = make_index_audit(
        connector.id, fixture.acme.id, status="failed", severity=None, created_at=NOW
    )
    embedding = make_index_audit(
        connector.id, fixture.acme.id, kind="embedding", created_at=NOW - timedelta(hours=1)
    )
    async with fixture.audits.begin(fixture.acme_scope) as transaction:
        for row in (older, newer, embedding):
            await transaction.add(row)
        await transaction.commit()

        latest = await transaction.latest(connector.id, "chunking")
        latest_embedding = await transaction.latest(connector.id, "embedding")
        found = await transaction.find(older.id)
        missing = await transaction.find(uuid.uuid4())

    assert latest is not None and latest.id == newer.id
    assert latest_embedding is not None and latest_embedding.id == embedding.id
    assert found is not None and found.id == older.id
    assert missing is None


@check
async def a_running_audit_is_found_and_a_finished_one_is_not(fixture: Fixture) -> None:
    connector = fixture.acme_connector
    running = make_index_audit(connector.id, fixture.acme.id, status="running", severity=None)
    async with fixture.audits.begin(fixture.acme_scope) as transaction:
        await transaction.add(make_index_audit(connector.id, fixture.acme.id))
        await transaction.add(running)
        await transaction.commit()
        assert (await transaction.running(connector.id, "chunking")) is not None
        assert (await transaction.running(connector.id, "embedding")) is None


@check
async def another_organizations_audits_are_invisible(fixture: Fixture) -> None:
    audit = make_index_audit(fixture.globex_connector.id, fixture.globex.id, severity="red")
    async with fixture.audits.begin(fixture.globex_scope) as transaction:
        await transaction.add(audit)
        await transaction.commit()
    async with fixture.audits.begin(fixture.acme_scope) as transaction:
        assert await transaction.find(audit.id) is None
        assert await transaction.latest(fixture.globex_connector.id, "chunking") is None
        assert all(row.organization_id == fixture.acme.id for row in await transaction.latest_all())


@check
async def latest_all_is_one_row_per_connector_and_kind(fixture: Fixture) -> None:
    connector = fixture.acme_connector
    rows = [
        make_index_audit(
            connector.id, fixture.acme.id, severity="red", created_at=NOW - timedelta(days=1)
        ),
        make_index_audit(connector.id, fixture.acme.id, severity="green", created_at=NOW),
        make_index_audit(
            connector.id, fixture.acme.id, kind="embedding", severity="amber", created_at=NOW
        ),
    ]
    async with fixture.audits.begin(fixture.acme_scope) as transaction:
        for row in rows:
            await transaction.add(row)
        await transaction.commit()
        latest = await transaction.latest_all()

    mine = [row for row in latest if row.connector_id == connector.id]
    assert sorted((row.kind, row.severity) for row in mine) == [
        ("chunking", "green"),
        ("embedding", "amber"),
    ]


# ---------------------------------------------------------------------------
# evaluation sets, items, runs
# ---------------------------------------------------------------------------


@check
async def sets_are_listed_per_gateway_in_creation_order_with_their_counts(
    fixture: Fixture,
) -> None:
    first = make_evaluation_set(fixture.acme_gateway, name="First")
    second = make_evaluation_set(fixture.acme_gateway, name="Second")
    second.created_at = first.created_at + timedelta(seconds=1)
    items = [
        make_evaluation_item(first, relevant=[{"chunk_id": "c1", "document_id": "d1"}]),
        make_evaluation_item(first, question="Who is on call?", relevant=[], verified=False),
        make_evaluation_item(
            first,
            question="Where is the office?",
            source="generated",
            verified=False,
            relevant=[{"chunk_id": "c2", "document_id": "d1"}],
        ),
    ]
    async with fixture.evaluations.begin(fixture.acme_scope) as transaction:
        await transaction.add_set(first)
        await transaction.add_set(second)
        for item in items:
            await transaction.add_item(item)
        await transaction.commit()

        listed = await transaction.sets(fixture.acme_gateway.id)
        counts = await transaction.item_counts([first.id, second.id])

    assert [row.name for row in listed if row.id in (first.id, second.id)] == ["First", "Second"]
    assert counts[first.id].total == 3
    assert counts[first.id].verified == 1
    assert counts[first.id].generated == 1
    assert counts[first.id].negatives == 1
    assert second.id not in counts


@check
async def items_come_back_in_creation_order_and_deleting_a_set_takes_them(
    fixture: Fixture,
) -> None:
    evaluation_set = make_evaluation_set(fixture.acme_gateway, name="Doomed")
    items = [make_evaluation_item(evaluation_set, question=f"q{n}?") for n in range(3)]
    for offset, item in enumerate(items):
        item.created_at = NOW + timedelta(seconds=offset)
    run = make_evaluation_run(evaluation_set)
    async with fixture.evaluations.begin(fixture.acme_scope) as transaction:
        await transaction.add_set(evaluation_set)
        for item in items:
            await transaction.add_item(item)
        await transaction.add_run(run)
        await transaction.commit()

        assert [row.question for row in await transaction.items(evaluation_set.id)] == [
            "q0?",
            "q1?",
            "q2?",
        ]
        found = await transaction.set(evaluation_set.id)
        assert found is not None
        await transaction.delete_set(found)
        await transaction.commit()

        assert await transaction.set(evaluation_set.id) is None
        assert await transaction.items(evaluation_set.id) == []
        assert await transaction.item(items[0].id) is None
        assert await transaction.run(run.id) is None


@check
async def runs_are_newest_first_and_bounded(fixture: Fixture) -> None:
    evaluation_set = make_evaluation_set(fixture.acme_gateway, name="History")
    runs = [make_evaluation_run(evaluation_set) for _ in range(3)]
    for offset, run in enumerate(runs):
        run.created_at = NOW + timedelta(minutes=offset)
    async with fixture.evaluations.begin(fixture.acme_scope) as transaction:
        await transaction.add_set(evaluation_set)
        for run in runs:
            await transaction.add_run(run)
        await transaction.commit()

        listed = await transaction.runs(evaluation_set.id)
        one = await transaction.runs(evaluation_set.id, limit=1)

    assert [run.id for run in listed] == [runs[2].id, runs[1].id, runs[0].id]
    assert [run.id for run in one] == [runs[2].id]


@check
async def another_organizations_sets_items_and_runs_are_invisible(fixture: Fixture) -> None:
    evaluation_set = make_evaluation_set(fixture.globex_gateway, name="Theirs")
    item = make_evaluation_item(evaluation_set)
    run = make_evaluation_run(evaluation_set)
    async with fixture.evaluations.begin(fixture.globex_scope) as transaction:
        await transaction.add_set(evaluation_set)
        await transaction.add_item(item)
        await transaction.add_run(run)
        await transaction.commit()

    async with fixture.evaluations.begin(fixture.acme_scope) as transaction:
        assert await transaction.set(evaluation_set.id) is None
        assert await transaction.item(item.id) is None
        assert await transaction.run(run.id) is None
        assert await transaction.items(evaluation_set.id) == []
        assert await transaction.runs(evaluation_set.id) == []
        assert await transaction.item_counts([evaluation_set.id]) == {}
        assert await transaction.sets(fixture.globex_gateway.id) == []


@check
async def a_run_keeps_its_results_and_snapshot_whole(fixture: Fixture) -> None:
    evaluation_set = make_evaluation_set(fixture.acme_gateway, name="Stored")
    run = make_evaluation_run(
        evaluation_set,
        patch={"doc_top_k": 3},
        config={"doc_top_k": 3, "connector_ids": [str(fixture.acme_connector.id)]},
        snapshot={
            "embedding_model": "hash-bow",
            "connectors": {str(fixture.acme_connector.id): {"fingerprints": {"abc": 4}}},
        },
        metrics={"k": 3, "all": {"chunk": {"recall": 0.5}}},
        results=[{"item_id": "x", "chunk": {"hit": True}}],
        total_items=1,
        completed_items=1,
    )
    async with fixture.evaluations.begin(fixture.acme_scope) as transaction:
        await transaction.add_set(evaluation_set)
        await transaction.add_run(run)
        await transaction.commit()
        stored = await transaction.run(run.id)

    assert stored is not None
    assert stored.patch == {"doc_top_k": 3}
    assert stored.metrics["all"]["chunk"]["recall"] == 0.5
    assert stored.results == [{"item_id": "x", "chunk": {"hit": True}}]
    assert stored.snapshot["connectors"][str(fixture.acme_connector.id)]["fingerprints"] == {
        "abc": 4
    }


__all__ = ["CHECKS", "Check", "Fixture"]
