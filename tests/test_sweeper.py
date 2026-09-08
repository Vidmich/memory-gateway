"""The orphan sweeper: what has no row behind it, and what it is allowed to do about it.

Two rules run through every test here, and they are the ones the task file singles out as
not being caution theatre.

**Report before delete.** The destructive pass acts on the set the report named, and
nothing else. So the tests assert the report *and* that a report-only run left every store
exactly as it found it — because a sweeper that quietly deleted while reporting would pass
any test that only checked the numbers.

**An upload in flight is not an orphan.** Bytes land in object storage before the document
row commits, so a sweeper with no age floor would race every upload it ever saw. That one
is a boundary test: an object a minute old and an object a day old, in the same sweep.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from app.core.ids import uuid7
from app.db.models import Connector, Document, MemoryFact, Organization
from tests.platform_support import PlatformFixture, build_platform

PREFIX = "orgs/acme/connectors/main/"


@pytest.fixture
def platform() -> PlatformFixture:
    return build_platform()


def seed_organization(platform: PlatformFixture) -> uuid.UUID:
    identifier = uuid7()
    platform.db.organizations[identifier] = Organization(
        id=identifier, name="Acme", slug=f"acme-{identifier.hex[:6]}"
    )
    connector = Connector(
        id=uuid7(),
        organization_id=identifier,
        name="main",
        type="managed_file_drop",
        config={},
        chunking={},
        storage_prefix=PREFIX,
        status="ready",
    )
    platform.db.add_connector(connector)
    return identifier


def seed_document(platform: PlatformFixture, organization_id: uuid.UUID, key: str) -> Document:
    connector = next(
        row for row in platform.db.connectors.values() if row.organization_id == organization_id
    )
    document = Document(
        id=uuid7(),
        organization_id=organization_id,
        connector_id=connector.id,
        source_uri=key,
        source_name=key.rsplit("/", 1)[-1],
        status="indexed",
    )
    platform.db.add_document(document)
    return document


async def store_object(platform: PlatformFixture, key: str, *, age_hours: float) -> None:
    """Put an object and age it.

    The age is set on the entry directly because the double has no clock to move, and the
    age floor is the whole subject of two of the tests below.
    """

    async def bytes_of() -> AsyncIterator[bytes]:
        yield b"some bytes"

    await platform.objects.put(key, bytes_of(), content_type="text/plain")
    entry = platform.objects.objects[key]
    entry.modified_at = datetime.now(UTC) - timedelta(hours=age_hours)


# ---------------------------------------------------------------------------
# vectors
# ---------------------------------------------------------------------------


async def test_a_chunk_whose_document_is_gone_is_reported(platform: PlatformFixture) -> None:
    organization = seed_organization(platform)
    kept = seed_document(platform, organization, f"{PREFIX}handbook.md")
    await platform.index_chunks(organization, kept.id, "kept text")
    ghost = uuid7()
    await platform.index_chunks(organization, ghost, "text with no row")

    report = await platform.sweeper.sweep()

    chunks = next(group for group in report.groups if group.kind == "document_points")
    assert chunks.ids == (str(ghost),)
    assert report.deleted == 0


async def test_a_report_only_sweep_deletes_nothing(platform: PlatformFixture) -> None:
    """The rule, asserted from the other side. A sweeper that deleted while reporting
    would satisfy every count-based test in this file."""
    organization = seed_organization(platform)
    ghost = uuid7()
    await platform.index_chunks(organization, ghost, "text with no row")
    before = await platform.vectors.count(organization)

    await platform.sweeper.sweep()

    assert await platform.vectors.count(organization) == before


async def test_the_destructive_pass_removes_exactly_the_reported_set(
    platform: PlatformFixture,
) -> None:
    organization = seed_organization(platform)
    kept = seed_document(platform, organization, f"{PREFIX}handbook.md")
    await platform.index_chunks(organization, kept.id, "kept one", "kept two")
    ghost = uuid7()
    await platform.index_chunks(organization, ghost, "orphan one", "orphan two")

    report = await platform.sweeper.sweep(apply=True)

    assert report.applied
    assert await platform.vectors.count(organization, document_id=ghost) == 0
    assert await platform.vectors.count(organization, document_id=kept.id) == 2


async def test_a_fact_vector_whose_row_is_gone_is_reported(platform: PlatformFixture) -> None:
    """The one that matters most: a fact with no row still shapes answers, and neither
    store can see it alone."""
    organization = seed_organization(platform)
    person = uuid7()
    live = MemoryFact(
        id=uuid7(),
        organization_id=organization,
        end_user_id=person,
        text="Prefers French.",
        kind="preference",
        confidence=1.0,
        created_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    platform.db.memory_facts[live.id] = live
    await platform.index_fact(organization, person, live.id, live.text)
    stranded = uuid7()
    await platform.index_fact(organization, person, stranded, "a fact nobody has a row for")

    report = await platform.sweeper.sweep(apply=True)

    facts = next(group for group in report.groups if group.kind == "fact_points")
    assert facts.ids == (str(stranded),)
    assert await platform.facts.ids(organization) == {str(live.id)}


# ---------------------------------------------------------------------------
# objects
# ---------------------------------------------------------------------------


async def test_a_stored_file_with_no_document_row_is_reported(
    platform: PlatformFixture,
) -> None:
    organization = seed_organization(platform)
    seed_document(platform, organization, f"{PREFIX}handbook.md")
    await store_object(platform, f"{PREFIX}handbook.md", age_hours=48)
    await store_object(platform, f"{PREFIX}forgotten.md", age_hours=48)

    report = await platform.sweeper.sweep()

    objects = next(group for group in report.groups if group.store == "object-store")
    assert objects.ids == (f"{PREFIX}forgotten.md",)


async def test_an_upload_in_flight_is_left_alone(platform: PlatformFixture) -> None:
    """An object whose document row has not committed yet looks exactly like an orphan.

    Without the age floor the sweeper would race every upload it ever saw — and the race
    is unrecoverable, because the bytes are what the row would have pointed at.
    """
    seed_organization(platform)
    await store_object(platform, f"{PREFIX}just-uploaded.md", age_hours=0.1)
    await store_object(platform, f"{PREFIX}long-forgotten.md", age_hours=48)

    report = await platform.sweeper.sweep(apply=True)

    objects = next(group for group in report.groups if group.store == "object-store")
    assert objects.ids == (f"{PREFIX}long-forgotten.md",)
    assert await platform.objects.head(f"{PREFIX}just-uploaded.md") is not None
    assert await platform.objects.head(f"{PREFIX}long-forgotten.md") is None


# ---------------------------------------------------------------------------
# scope and reporting
# ---------------------------------------------------------------------------


async def test_a_clean_platform_reports_nothing(platform: PlatformFixture) -> None:
    organization = seed_organization(platform)
    document = seed_document(platform, organization, f"{PREFIX}handbook.md")
    await platform.index_chunks(organization, document.id, "text")
    await store_object(platform, f"{PREFIX}handbook.md", age_hours=48)

    report = await platform.sweeper.sweep()

    assert report.total == 0
    assert report.groups == []
    assert report.organizations == 1


async def test_a_sweep_can_be_narrowed_to_one_organization(
    platform: PlatformFixture,
) -> None:
    first = seed_organization(platform)
    second = seed_organization(platform)
    await platform.index_chunks(first, uuid7(), "orphan in the first")
    await platform.index_chunks(second, uuid7(), "orphan in the second")

    report = await platform.sweeper.sweep(organization_id=first)

    assert report.organizations == 1
    assert report.total == 1


async def test_the_run_is_recorded_with_its_report(platform: PlatformFixture) -> None:
    organization = seed_organization(platform)
    await platform.index_chunks(organization, uuid7(), "orphan")

    await platform.sweeper.sweep()

    async with platform.store.begin() as transaction:
        runs = [run for run in await transaction.recent_runs(limit=5) if run.job == "sweep"]
    assert runs[0].status == "succeeded"
    assert runs[0].report["orphans"] == 1
    assert runs[0].report["applied"] is False


async def test_the_report_samples_rather_than_exports(platform: PlatformFixture) -> None:
    """A response carrying ten thousand point ids is a data export nobody asked for."""
    organization = seed_organization(platform)
    for _ in range(25):
        await platform.index_chunks(organization, uuid7(), "orphan")

    report = await platform.sweeper.sweep()
    rendered = next(
        group for group in report.as_json()["groups"] if group["kind"] == "document_points"
    )

    assert rendered["count"] == 25
    assert len(rendered["sample"]) == 10
