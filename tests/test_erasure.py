"""Erasure across all three stores, and the artefact that says so.

SPEC §6.5's deletion path was built in task 12. What is asserted here is the part task 17
adds: that the *report* is produced by looking, not by counting what was sent. Those differ
exactly when it matters — a Qdrant delete that failed and was swallowed, a filter that
missed points an older build wrote — so every test asserts a store's contents rather than a
service's return value wherever it can.

The organization tests are about the grace period, which is the only window in which
"we deleted the wrong tenant" is recoverable: the destructive pass drops collections and
object-store prefixes that no database backup contains.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import NotFound, Validation
from app.core.ids import uuid7
from app.core.tenancy import Actor, TenantScope
from app.db.models import Connector, Document, Gateway, MemoryFact, Organization, RequestLog
from app.services.erasure import (
    ORGANIZATION_DELETE_CANCELLED,
    ORGANIZATION_DELETE_REQUESTED,
    ORGANIZATION_PURGED,
)
from tests.gateway_support import make_gateway_row
from tests.platform_support import PlatformFixture, build_platform

PREFIX = "orgs/acme/connectors/main/"


@pytest.fixture
def platform() -> PlatformFixture:
    return build_platform()


@pytest.fixture
def actor() -> Actor:
    return Actor(
        user_id=uuid7(),
        scope=TenantScope(role="superadmin", organization_id=None),
        label="ops@example.com",
    )


async def seed_tenant(platform: PlatformFixture) -> Organization:
    """One organization with something in every store."""
    organization = Organization(id=uuid7(), name="Acme", slug="acme", status="active")
    platform.db.organizations[organization.id] = organization

    connector = Connector(
        id=uuid7(),
        organization_id=organization.id,
        name="main",
        type="managed_file_drop",
        config={},
        chunking={},
        storage_prefix=PREFIX,
        status="ready",
    )
    platform.db.add_connector(connector)
    document = Document(
        id=uuid7(),
        organization_id=organization.id,
        connector_id=connector.id,
        source_uri=f"{PREFIX}handbook.md",
        source_name="handbook.md",
        status="indexed",
    )
    platform.db.add_document(document)
    await platform.index_chunks(organization.id, document.id, "a chunk about refunds")

    person = uuid7()
    fact = MemoryFact(
        id=uuid7(),
        organization_id=organization.id,
        end_user_id=person,
        text="Prefers French.",
        kind="preference",
        confidence=1.0,
        created_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    platform.db.memory_facts[fact.id] = fact
    await platform.index_fact(organization.id, person, fact.id, fact.text)

    gateway: Gateway = make_gateway_row(organization, slug="chat")
    platform.db.add_gateway(gateway)
    log_id = uuid7()
    platform.db.request_logs[log_id] = RequestLog(
        id=log_id,
        created_at=datetime.now(UTC),
        organization_id=organization.id,
        gateway_id=gateway.id,
        status_code=200,
    )

    async def bytes_of() -> AsyncIterator[bytes]:
        yield b"# Handbook"

    await platform.objects.put(f"{PREFIX}handbook.md", bytes_of(), content_type="text/markdown")
    return organization


# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------


async def test_a_deletion_request_destroys_nothing(platform: PlatformFixture, actor: Actor) -> None:
    organization = await seed_tenant(platform)

    purge_after = await platform.eraser.request(actor, organization.id, confirm="acme")

    assert purge_after > datetime.now(UTC)
    assert platform.db.organizations[organization.id].status == "deleting"
    assert await platform.vectors.count(organization.id) == 1
    assert await platform.objects.head(f"{PREFIX}handbook.md") is not None


async def test_the_slug_has_to_be_typed(platform: PlatformFixture, actor: Actor) -> None:
    """SPEC §13.2's typed confirmation, in the API rather than only in the form: a script
    with a bearer token should have to mean it too."""
    organization = await seed_tenant(platform)

    with pytest.raises(Validation) as refused:
        await platform.eraser.request(actor, organization.id, confirm="acme corp")

    assert refused.value.param == "confirm"
    assert platform.db.organizations[organization.id].status == "active"


async def test_a_deleting_organization_stops_being_usable(
    platform: PlatformFixture, actor: Actor
) -> None:
    """The same effect ``suspended`` has, and through the same property, so no gateway or
    login check has to learn a second state."""
    organization = await seed_tenant(platform)

    await platform.eraser.request(actor, organization.id, confirm="acme")

    assert platform.db.organizations[organization.id].is_active is False


async def test_a_request_can_be_taken_back(platform: PlatformFixture, actor: Actor) -> None:
    organization = await seed_tenant(platform)
    await platform.eraser.request(actor, organization.id, confirm="acme")

    await platform.eraser.cancel(actor, organization.id)

    assert platform.db.organizations[organization.id].status == "active"
    assert platform.db.organizations[organization.id].purge_after is None


async def test_cancelling_something_that_was_never_scheduled_is_a_404(
    platform: PlatformFixture, actor: Actor
) -> None:
    organization = await seed_tenant(platform)

    with pytest.raises(NotFound):
        await platform.eraser.cancel(actor, organization.id)


async def test_nothing_is_due_until_the_grace_period_has_run_out(
    platform: PlatformFixture, actor: Actor
) -> None:
    organization = await seed_tenant(platform)
    await platform.eraser.request(actor, organization.id, confirm="acme", grace_days=7)

    assert await platform.eraser.due() == []
    assert await platform.eraser.due(now=datetime.now(UTC) + timedelta(days=8)) == [organization.id]


async def test_an_immediate_deletion_is_due_at_once(
    platform: PlatformFixture, actor: Actor
) -> None:
    """Zero days is the escape hatch for a customer who has asked in writing."""
    organization = await seed_tenant(platform)
    await platform.eraser.request(actor, organization.id, confirm="acme", grace_days=0)

    assert await platform.eraser.due(now=datetime.now(UTC) + timedelta(seconds=1)) == [
        organization.id
    ]


# ---------------------------------------------------------------------------
# the destructive pass
# ---------------------------------------------------------------------------


async def test_a_purge_leaves_zero_rows_and_zero_points_across_every_store(
    platform: PlatformFixture, actor: Actor
) -> None:
    """The acceptance criterion, asserted by looking at each store rather than by trusting
    the count the pass returned."""
    organization = await seed_tenant(platform)
    await platform.eraser.request(actor, organization.id, confirm="acme", grace_days=0)

    report = await platform.eraser.purge(organization.id)

    assert report.complete
    assert organization.id not in platform.db.organizations
    assert not platform.db.documents
    assert not platform.db.memory_facts
    assert not platform.db.request_logs
    assert await platform.vectors.count(organization.id) == 0
    assert await platform.facts.ids(organization.id) == set()
    assert await platform.objects.head(f"{PREFIX}handbook.md") is None


async def test_the_report_names_each_store(platform: PlatformFixture, actor: Actor) -> None:
    """The artefact you hand somebody who asks whether a deletion request was honoured.

    Since task 19 the vector entry names the *backend*: a report that said "qdrant" for a
    tenant whose vectors were in Chroma would be exactly the kind of document that is
    worthless for the purpose it exists for.
    """
    organization = await seed_tenant(platform)
    await platform.eraser.request(actor, organization.id, confirm="acme", grace_days=0)

    report = await platform.eraser.purge(organization.id)

    assert {entry.store for entry in report.stores} == {
        "postgres",
        "vectors:qdrant",
        "object-store",
    }
    assert report.subject == "acme"
    assert next(entry for entry in report.stores if entry.store == "object-store").removed == 1


async def test_the_record_of_the_deletion_outlives_the_organization(
    platform: PlatformFixture, actor: Actor
) -> None:
    """Deliberately the one place a deletion is incomplete. "Who deleted this tenant, and
    when" is the question a deletion record exists to answer, so the event goes into the
    platform's log rather than into the log that is going with them."""
    organization = await seed_tenant(platform)
    await platform.eraser.request(actor, organization.id, confirm="acme", grace_days=0)

    await platform.eraser.purge(organization.id)

    events = [row.action for row in platform.db.audit_events.values()]
    assert ORGANIZATION_DELETE_REQUESTED in events
    assert ORGANIZATION_PURGED in events
    purge = next(
        row for row in platform.db.audit_events.values() if row.action == ORGANIZATION_PURGED
    )
    assert purge.organization_id is None
    assert purge.target_label == "acme"


async def test_cancelling_is_recorded_too(platform: PlatformFixture, actor: Actor) -> None:
    organization = await seed_tenant(platform)
    await platform.eraser.request(actor, organization.id, confirm="acme")

    await platform.eraser.cancel(actor, organization.id)

    actions = [row.action for row in platform.db.audit_events.values()]
    assert ORGANIZATION_DELETE_CANCELLED in actions


async def test_another_organizations_data_is_untouched(
    platform: PlatformFixture, actor: Actor
) -> None:
    doomed = await seed_tenant(platform)
    neighbour = Organization(id=uuid7(), name="Globex", slug="globex", status="active")
    platform.db.organizations[neighbour.id] = neighbour
    await platform.index_chunks(neighbour.id, uuid7(), "a chunk belonging to somebody else")

    await platform.eraser.request(actor, doomed.id, confirm="acme", grace_days=0)
    await platform.eraser.purge(doomed.id)

    assert neighbour.id in platform.db.organizations
    assert await platform.vectors.count(neighbour.id) == 1


async def test_purging_an_organization_that_is_already_gone_is_a_404(
    platform: PlatformFixture,
) -> None:
    with pytest.raises(NotFound):
        await platform.eraser.purge(uuid7())


async def test_the_scheduled_pass_purges_everything_that_is_due(
    platform: PlatformFixture, actor: Actor
) -> None:
    organization = await seed_tenant(platform)
    await platform.eraser.request(actor, organization.id, confirm="acme", grace_days=0)

    reports = await platform.service.purge_due()

    assert [report.subject for report in reports] == ["acme"]
    assert organization.id not in platform.db.organizations


async def test_the_scheduled_pass_leaves_a_tenant_still_inside_its_grace_period(
    platform: PlatformFixture, actor: Actor
) -> None:
    organization = await seed_tenant(platform)
    await platform.eraser.request(actor, organization.id, confirm="acme", grace_days=30)

    assert await platform.service.purge_due() == []
    assert organization.id in platform.db.organizations
