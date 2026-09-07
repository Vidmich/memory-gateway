"""One set of assertions for the connector store, run against memory and PostgreSQL.

Same shape and same reasons as ``tests/gateway_store_contract.py``. The check that earns
this file, though, is :func:`claiming_the_same_object_twice_produces_one_document`: it is
an ``ON CONFLICT`` upsert in PostgreSQL and a dictionary lookup in memory, and those are
exactly the kind of pair that drift. Running the same assertion through both is what makes
the fast one worth trusting.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.tenancy import TenantScope
from app.db.models import Connector, Organization
from app.services.connector_store import ConnectorStore, DocumentDraft


@dataclass(frozen=True)
class Fixture:
    store: ConnectorStore
    acme: Organization
    globex: Organization
    acme_connector: Connector
    globex_connector: Connector

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


def draft(name: str = "handbook.md", *, etag: str = "e1", size: int = 100) -> DocumentDraft:
    return DocumentDraft(
        source_uri=f"orgs/x/connectors/y/{name}",
        source_name=name,
        size_bytes=size,
        etag=etag,
        mime_type="text/markdown",
    )


# ---------------------------------------------------------------------------
# scoping
# ---------------------------------------------------------------------------


@check
async def a_connector_is_readable_inside_its_own_organization(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.connector(fixture.acme_connector.id) is not None


@check
async def another_organizations_connector_is_not_found(fixture: Fixture) -> None:
    """404, not 403. The caller turns ``None`` into a not-found, which is why cross-tenant
    access cannot be distinguished from a typo."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.connector(fixture.globex_connector.id) is None


@check
async def a_listing_never_crosses_organizations(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        rows = await transaction.connectors(after=None, limit=50)

    assert [row.id for row in rows] == [fixture.acme_connector.id]


@check
async def a_name_is_unique_only_inside_its_organization(fixture: Fixture) -> None:
    """Two customers may both call a connector "Product docs"; neither may have two."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.name_taken(fixture.acme_connector.name) is True
        assert await transaction.name_taken(fixture.globex_connector.name) is False


@check
async def a_name_check_can_exclude_the_row_being_edited(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        taken = await transaction.name_taken(
            fixture.acme_connector.name, excluding=fixture.acme_connector.id
        )

    assert taken is False


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------


@check
async def claiming_an_object_creates_a_document(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        connector = await transaction.connector(fixture.acme_connector.id)
        assert connector is not None
        document = await transaction.claim_document(connector, draft(), reset=True)
        await transaction.commit()

    assert document.status == "pending"
    assert document.source_name == "handbook.md"
    assert document.organization_id == fixture.acme.id


@check
async def claiming_the_same_object_twice_produces_one_document(fixture: Fixture) -> None:
    """The acceptance criterion, at the level where it is actually decided. A ``SELECT``
    then ``INSERT`` produces either a duplicate row or an integrity error depending on
    timing; the unique constraint plus ``ON CONFLICT`` makes the database decide."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        connector = await transaction.connector(fixture.acme_connector.id)
        assert connector is not None
        first = await transaction.claim_document(connector, draft(), reset=True)
        second = await transaction.claim_document(connector, draft(etag="e2"), reset=True)
        await transaction.commit()

        rows = await transaction.documents(connector.id, after=None, limit=50)

    assert first.id == second.id
    assert len(rows) == 1
    assert second.etag == "e2"


@check
async def a_reset_clears_the_previous_runs_result(fixture: Fixture) -> None:
    """A row that says ``pending`` while still showing the last error and chunk count is
    a screen nobody can read."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        connector = await transaction.connector(fixture.acme_connector.id)
        assert connector is not None
        document = await transaction.claim_document(connector, draft(), reset=True)
        document.status = "failed"
        document.error = "could not decode"
        document.chunk_count = 7
        await transaction.commit()

        again = await transaction.claim_document(connector, draft(etag="e2"), reset=True)
        await transaction.commit()

    assert again.status == "pending"
    assert again.error is None
    assert again.chunk_count == 0


@check
async def claiming_without_a_reset_leaves_the_row_alone(fixture: Fixture) -> None:
    """What a resync wants for an object it has decided not to re-ingest."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        connector = await transaction.connector(fixture.acme_connector.id)
        assert connector is not None
        document = await transaction.claim_document(connector, draft(), reset=True)
        document.status = "indexed"
        document.chunk_count = 4
        await transaction.commit()

        again = await transaction.claim_document(connector, draft(etag="e2"), reset=False)

    assert again.status == "indexed"
    assert again.chunk_count == 4


@check
async def a_document_is_found_by_its_source_uri(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        connector = await transaction.connector(fixture.acme_connector.id)
        assert connector is not None
        created = await transaction.claim_document(connector, draft(), reset=True)
        await transaction.commit()

        found = await transaction.document_by_source(connector.id, created.source_uri)

    assert found is not None and found.id == created.id


@check
async def another_organizations_document_is_not_found(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.globex_scope) as transaction:
        connector = await transaction.connector(fixture.globex_connector.id)
        assert connector is not None
        theirs = await transaction.claim_document(connector, draft("secret.md"), reset=True)
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.document(theirs.id) is None


@check
async def documents_can_be_filtered_by_status(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        connector = await transaction.connector(fixture.acme_connector.id)
        assert connector is not None
        good = await transaction.claim_document(connector, draft("a.md"), reset=True)
        good.status = "indexed"
        await transaction.claim_document(connector, draft("b.md"), reset=True)
        await transaction.commit()

        rows = await transaction.documents(connector.id, after=None, limit=50, status="indexed")

    assert [row.source_name for row in rows] == ["a.md"]


@check
async def the_index_carries_what_reconciliation_compares(fixture: Fixture) -> None:
    """Four columns, not whole rows: a connector with fifty thousand documents is
    reconciled in one pass, and loading fifty thousand ORM objects to compare two strings
    each is how that becomes a memory incident."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        connector = await transaction.connector(fixture.acme_connector.id)
        assert connector is not None
        document = await transaction.claim_document(connector, draft(etag="etag-1"), reset=True)
        document.status = "indexed"
        await transaction.commit()

        rows = await transaction.index(connector.id)

    assert rows == [(document.id, document.source_uri, "etag-1", "indexed")]


@check
async def counts_are_grouped_by_status(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        connector = await transaction.connector(fixture.acme_connector.id)
        assert connector is not None
        first = await transaction.claim_document(connector, draft("a.md"), reset=True)
        first.status = "indexed"
        second = await transaction.claim_document(connector, draft("b.md"), reset=True)
        second.status = "indexed"
        third = await transaction.claim_document(connector, draft("c.md"), reset=True)
        third.status = "failed"
        await transaction.commit()

        counts = await transaction.document_counts([connector.id])

    assert dict(counts[connector.id]) == {"indexed": 2, "failed": 1}


@check
async def counts_are_empty_for_a_connector_with_no_documents(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        counts = await transaction.document_counts([fixture.acme_connector.id])

    assert counts.get(fixture.acme_connector.id, {}) == {}


@check
async def sizes_are_summed_per_connector(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        connector = await transaction.connector(fixture.acme_connector.id)
        assert connector is not None
        await transaction.claim_document(connector, draft("a.md", size=100), reset=True)
        await transaction.claim_document(connector, draft("b.md", size=250), reset=True)
        await transaction.commit()

        sizes = await transaction.document_bytes([connector.id])

    assert sizes[connector.id] == 350


@check
async def the_organization_total_backs_the_quota_check(fixture: Fixture) -> None:
    """Read from the document rows rather than by listing every object under every prefix,
    which would be O(objects) network calls on the request path."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        connector = await transaction.connector(fixture.acme_connector.id)
        assert connector is not None
        await transaction.claim_document(connector, draft("a.md", size=400), reset=True)
        await transaction.commit()

        total = await transaction.organization_bytes()

    assert total == 400


@check
async def the_organization_total_never_counts_another_tenant(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.globex_scope) as transaction:
        connector = await transaction.connector(fixture.globex_connector.id)
        assert connector is not None
        await transaction.claim_document(connector, draft("theirs.md", size=9999), reset=True)
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.organization_bytes() == 0


@check
async def deleting_a_document_removes_it(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        connector = await transaction.connector(fixture.acme_connector.id)
        assert connector is not None
        document = await transaction.claim_document(connector, draft(), reset=True)
        await transaction.commit()

        await transaction.delete_document(document)
        await transaction.commit()

        assert await transaction.document(document.id) is None


@check
async def deleting_a_connector_takes_its_documents(fixture: Fixture) -> None:
    """``ON DELETE CASCADE`` in the schema, and the same behaviour in memory — so a test
    cannot pass against orphaned rows the database would have removed."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        connector = await transaction.connector(fixture.acme_connector.id)
        assert connector is not None
        document = await transaction.claim_document(connector, draft(), reset=True)
        await transaction.commit()

        await transaction.delete_connector(connector)
        await transaction.commit()

        assert await transaction.document(document.id) is None


@check
async def a_page_stops_at_the_limit_and_offers_a_cursor(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        connector = await transaction.connector(fixture.acme_connector.id)
        assert connector is not None
        for index in range(4):
            await transaction.claim_document(connector, draft(f"f{index}.md"), reset=True)
        await transaction.commit()

        rows = await transaction.documents(connector.id, after=None, limit=2)

    # `limit + 1`, so the caller can tell there is another page without a second COUNT.
    assert len(rows) == 3


@check
async def a_cursor_walks_backwards_through_the_ids(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        connector = await transaction.connector(fixture.acme_connector.id)
        assert connector is not None
        ids = [
            (await transaction.claim_document(connector, draft(f"f{index}.md"), reset=True)).id
            for index in range(4)
        ]
        await transaction.commit()

        page = await transaction.documents(connector.id, after=ids[2], limit=10)

    assert {row.id for row in page} == {ids[0], ids[1]}


def uuid_that_does_not_exist() -> uuid.UUID:
    return uuid.uuid4()


@check
async def a_connector_that_does_not_exist_is_none(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.connector(uuid_that_does_not_exist()) is None


__all__ = ["CHECKS", "Check", "Fixture", "draft"]
