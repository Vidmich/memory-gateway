"""The summarization ledger's contract (task 102), run against both implementations.

Two checks earn the file. The cap's numerator must not count refusals, or the cap latches
on for the rest of the day; and the panel's totals must be the sum of the rows, per day,
per model and per connector, scoped to one organization — because the in-memory twin and
the SQL each get grouping subtly wrong in their own way, and the number on the screen is
the number that will be quoted back as a bill.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.tenancy import TenantScope
from app.db.models import Connector, Organization
from app.services.summarization_store import (
    DAILY_CAP,
    FAILED,
    SKIPPED,
    SUCCEEDED,
    WAITING_ON_CAP,
    RunRecord,
    SummarizationStore,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Fixture:
    store: SummarizationStore
    acme: Organization
    globex: Organization
    acme_connector: Connector
    acme_other: Connector
    globex_connector: Connector

    def scope(self, organization: Organization) -> TenantScope:
        return TenantScope(role="org_admin", organization_id=organization.id)

    @property
    def acme_scope(self) -> TenantScope:
        return self.scope(self.acme)


Check = Callable[[Fixture], Awaitable[None]]
CHECKS: list[Check] = []


def check(function: Check) -> Check:
    CHECKS.append(function)
    return function


def run_of(fixture: Fixture, **overrides: object) -> RunRecord:
    values: dict[str, object] = {
        "organization_id": fixture.acme.id,
        "connector_id": fixture.acme_connector.id,
        "document_id": uuid.uuid4(),
        "outcome": SUCCEEDED,
        "model_id": uuid.uuid4(),
        "model_name": "cheap",
        "tokens_in": 100,
        "tokens_out": 20,
    }
    values.update(overrides)
    return RunRecord(**values)  # type: ignore[arg-type]


async def seed(fixture: Fixture) -> None:
    """Three attempts on acme's first connector, one on its second, one on globex's."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        await transaction.record(run_of(fixture))
        await transaction.record(run_of(fixture, outcome=FAILED, reason="provider_refused"))
        await transaction.record(
            run_of(fixture, outcome=SKIPPED, reason=DAILY_CAP, tokens_in=0, tokens_out=0)
        )
        await transaction.record(
            run_of(
                fixture,
                connector_id=fixture.acme_other.id,
                model_name="big",
                tokens_in=1000,
                tokens_out=50,
                estimated=True,
            )
        )
        await transaction.commit()
    async with fixture.store.begin(fixture.scope(fixture.globex)) as transaction:
        await transaction.record(
            run_of(
                fixture,
                organization_id=fixture.globex.id,
                connector_id=fixture.globex_connector.id,
                tokens_in=5000,
            )
        )
        await transaction.commit()


@check
async def the_cap_counts_attempts_that_reached_the_model_and_not_refusals(
    fixture: Fixture,
) -> None:
    await seed(fixture)
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        counted = await transaction.documents_since(
            fixture.acme_connector.id, datetime.now(UTC) - timedelta(hours=1)
        )
        other = await transaction.documents_since(
            fixture.acme_other.id, datetime.now(UTC) - timedelta(hours=1)
        )
        stale = await transaction.documents_since(
            fixture.acme_connector.id, datetime.now(UTC) + timedelta(hours=1)
        )

    assert counted == 2  # succeeded + failed; the skip does not count
    assert other == 1
    assert stale == 0


@check
async def the_panel_sums_the_rows_per_day_model_and_connector(fixture: Fixture) -> None:
    await seed(fixture)
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        health = await transaction.health(
            start=datetime.now(UTC) - timedelta(days=1), end=datetime.now(UTC) + timedelta(hours=1)
        )

    assert health.runs == 4
    assert health.documents == 2
    assert health.failures == 1
    assert health.capped == 1
    assert health.tokens_in == 1200
    assert health.tokens_out == 90
    assert health.estimated_runs == 1
    assert health.failure_rate == 0.25
    [day] = health.days
    assert (day.documents, day.failures, day.capped, day.tokens_in) == (2, 1, 1, 1200)
    # Biggest spender first, by name.
    assert [(m.model_name, m.runs, m.tokens_in) for m in health.by_model] == [
        ("big", 1, 1000),
        ("cheap", 2, 200),
    ]
    assert [(c.connector_id, c.name, c.documents, c.tokens_in) for c in health.top_connectors] == [
        (fixture.acme_other.id, fixture.acme_other.name, 1, 1000),
        (fixture.acme_connector.id, fixture.acme_connector.name, 1, 200),
    ]


@check
async def the_panel_is_scoped_to_one_organization(fixture: Fixture) -> None:
    await seed(fixture)
    async with fixture.store.begin(fixture.scope(fixture.globex)) as transaction:
        health = await transaction.health(
            start=datetime.now(UTC) - timedelta(days=1), end=datetime.now(UTC) + timedelta(hours=1)
        )

    assert health.runs == 1
    assert health.tokens_in == 5000
    assert [c.connector_id for c in health.top_connectors] == [fixture.globex_connector.id]


@check
async def the_panel_can_be_narrowed_to_one_connector(fixture: Fixture) -> None:
    await seed(fixture)
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        health = await transaction.health(
            start=datetime.now(UTC) - timedelta(days=1),
            end=datetime.now(UTC) + timedelta(hours=1),
            connector_id=fixture.acme_other.id,
        )

    assert health.runs == 1
    assert health.tokens_in == 1000
    assert [m.model_name for m in health.by_model] == ["big"]


@check
async def a_window_that_excludes_everything_finds_nothing(fixture: Fixture) -> None:
    await seed(fixture)
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        health = await transaction.health(
            start=NOW - timedelta(days=400), end=NOW - timedelta(days=399)
        )

    assert health.runs == 0
    assert health.days == ()
    assert health.by_model == ()


@check
async def a_reason_longer_than_the_column_is_cut_not_refused(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        row = await transaction.record(run_of(fixture, outcome=FAILED, reason="x" * 2000))
        await transaction.commit()

    assert row.reason is not None and len(row.reason) == 500


@check
async def documents_parked_on_the_cap_are_counted_per_connector(fixture: Fixture) -> None:
    """Read off ``documents``, not the ledger: the row that says ``pending`` with the cap's
    reason is the cap's visible consequence."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        health = await transaction.health(
            start=datetime.now(UTC) - timedelta(days=1), end=datetime.now(UTC) + timedelta(hours=1)
        )

    assert [(w.connector_id, w.name, w.documents) for w in health.waiting] == [
        (fixture.acme_connector.id, fixture.acme_connector.name, 2)
    ]
    assert health.waiting_documents == 2


__all__ = ["CHECKS", "NOW", "WAITING_ON_CAP", "Check", "Fixture", "run_of", "seed"]
