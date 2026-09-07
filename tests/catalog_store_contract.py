"""The behaviour every catalog store must have, written once.

Same arrangement as ``tests/directory_store_contract.py``: the in-memory store is only
worth having if it answers like PostgreSQL does, so the questions
:class:`~app.services.catalog.CatalogService` asks are asked here and both
implementations run the same list.

The subject is three models — one per organization plus one owned by nobody — because
the whole point of this table is that it has *two* scopes. A fixture with one
organization would let "own models only" and "own models plus the global catalog" pass
identically.

Not a test module itself; it is the shared body the two halves parametrize over.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.ids import uuid7
from app.core.tenancy import TenantScope
from app.db.models import Gateway, Organization, UpstreamModel
from app.services.catalog_store import CatalogStore
from tests.catalog_support import make_model


@dataclass
class Fixture:
    """Two organizations with a model each, one global model, one gateway on Acme's."""

    store: CatalogStore
    acme: Organization
    globex: Organization
    acme_model: UpstreamModel
    globex_model: UpstreamModel
    global_model: UpstreamModel
    acme_gateway: Gateway

    @property
    def acme_scope(self) -> TenantScope:
        return TenantScope(role="org_admin", organization_id=self.acme.id)

    @property
    def globex_scope(self) -> TenantScope:
        return TenantScope(role="org_admin", organization_id=self.globex.id)

    @property
    def platform_scope(self) -> TenantScope:
        return TenantScope(role="superadmin", organization_id=None)


# ---------------------------------------------------------------------------
# reading: the wide view
# ---------------------------------------------------------------------------


async def an_organization_reads_its_own_model(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        found = await transaction.model(fixture.acme_model.id)

    assert found is not None and found.id == fixture.acme_model.id


async def an_organization_reads_the_global_catalog(fixture: Fixture) -> None:
    """The widening that makes this table different from every other one."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        found = await transaction.model(fixture.global_model.id)

    assert found is not None and found.organization_id is None


async def an_organization_cannot_read_anothers_model(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        found = await transaction.model(fixture.globex_model.id)

    assert found is None


async def a_platform_scope_reads_everything(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        for model in (fixture.acme_model, fixture.globex_model, fixture.global_model):
            assert await transaction.model(model.id) is not None


async def listing_is_own_plus_global(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        rows = await transaction.models(after=None, limit=50)

    assert {row.id for row in rows} == {fixture.acme_model.id, fixture.global_model.id}


async def listing_is_newest_first(fixture: Fixture) -> None:
    """UUIDv7 ids sort by creation time, which is what makes the cursor just an id."""
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        rows = await transaction.models(after=None, limit=50)

    assert [row.id for row in rows] == sorted((row.id for row in rows), reverse=True)


async def a_cursor_skips_what_came_before_it(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        first = await transaction.models(after=None, limit=1)
        rest = await transaction.models(after=first[0].id, limit=50)

    assert first[0].id not in {row.id for row in rest}


async def the_scope_filter_selects_the_global_catalog(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        rows = await transaction.models(after=None, limit=50, scope_filter="global")

    assert [row.id for row in rows] == [fixture.global_model.id]


async def the_scope_filter_selects_own_models(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        rows = await transaction.models(after=None, limit=50, scope_filter="org")

    assert [row.id for row in rows] == [fixture.acme_model.id]


async def a_disabled_catalog_model_is_hidden_from_a_tenant(fixture: Fixture) -> None:
    """It cannot serve a request, so offering it would only invite somebody to point a
    gateway at something switched off."""
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        model = await transaction.owned_model(fixture.global_model.id)
        assert model is not None
        model.enabled = False
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.model(fixture.global_model.id) is None
        rows = await transaction.models(after=None, limit=50)

    assert fixture.global_model.id not in {row.id for row in rows}


async def the_platform_still_sees_a_disabled_catalog_model(fixture: Fixture) -> None:
    """Somebody has to be able to switch it back on."""
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        model = await transaction.owned_model(fixture.global_model.id)
        assert model is not None
        model.enabled = False
        await transaction.commit()

    async with fixture.store.begin(fixture.platform_scope) as transaction:
        assert await transaction.model(fixture.global_model.id) is not None


async def a_disabled_model_of_your_own_is_still_visible(fixture: Fixture) -> None:
    """Only the *catalog* is filtered. Hiding an organization's own disabled model would
    make it unrecoverable from the screen that disabled it."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        model = await transaction.owned_model(fixture.acme_model.id)
        assert model is not None
        model.enabled = False
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.model(fixture.acme_model.id) is not None


async def the_enabled_filter_excludes_disabled_models(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        model = await transaction.owned_model(fixture.acme_model.id)
        assert model is not None
        model.enabled = False
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        enabled = await transaction.models(after=None, limit=50, enabled=True)
        disabled = await transaction.models(after=None, limit=50, enabled=False)

    assert fixture.acme_model.id not in {row.id for row in enabled}
    assert [row.id for row in disabled] == [fixture.acme_model.id]


# ---------------------------------------------------------------------------
# reading: the narrow view
# ---------------------------------------------------------------------------


async def an_organization_owns_its_own_model(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.owned_model(fixture.acme_model.id) is not None


async def an_organization_does_not_own_a_global_model(fixture: Fixture) -> None:
    """The rule that makes "an org user cannot edit the global catalog" a 404 rather than
    a role check on three separate routes."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.owned_model(fixture.global_model.id) is None


async def the_platform_owns_a_global_model(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        assert await transaction.owned_model(fixture.global_model.id) is not None


async def an_assumed_scope_no_longer_owns_a_global_model(fixture: Fixture) -> None:
    """A superadmin who has opened an organization is inside it for every purpose. To
    edit the global catalog they leave first, which keeps "which hat am I wearing"
    answerable from the banner alone."""
    assumed = TenantScope(role="superadmin", organization_id=fixture.acme.id, assumed=True)
    async with fixture.store.begin(assumed) as transaction:
        assert await transaction.owned_model(fixture.global_model.id) is None


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------


async def a_name_is_taken_within_the_organization(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.name_taken(fixture.acme_model.name) is True


async def another_organizations_name_is_free(fixture: Fixture) -> None:
    """Two customers may both call a model ``gpt-4o``; the constraint is per
    organization, and a check that said otherwise would leak that the other one exists."""
    async with fixture.store.begin(fixture.globex_scope) as transaction:
        assert await transaction.name_taken(fixture.acme_model.name) is False


async def a_name_can_exclude_the_row_being_edited(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        taken = await transaction.name_taken(
            fixture.acme_model.name, excluding=fixture.acme_model.id
        )

    assert taken is False


async def a_global_name_is_taken_from_any_scope(fixture: Fixture) -> None:
    """The global namespace is one namespace, so the check cannot ride on the scope: a
    superadmin who has opened an organization still writes into it."""
    assumed = TenantScope(role="superadmin", organization_id=fixture.acme.id, assumed=True)
    for scope in (fixture.platform_scope, assumed):
        async with fixture.store.begin(scope) as transaction:
            assert await transaction.global_name_taken(fixture.global_model.name) is True


async def an_org_models_name_does_not_occupy_the_global_namespace(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        assert await transaction.global_name_taken(fixture.acme_model.name) is False


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


async def adding_a_model_stamps_the_scope(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        # Deliberately handing it the *wrong* organization: the repository overwrites it
        # rather than defaulting, so a caller cannot choose.
        added = await transaction.add_model(
            make_model(organization=fixture.globex, name="freshly-added")
        )
        await transaction.commit()

    assert added.organization_id == fixture.acme.id


async def adding_a_global_model_owns_it_to_nobody(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        added = await transaction.add_global_model(
            make_model(organization=fixture.acme, name="freshly-shared")
        )
        await transaction.commit()

    assert added.organization_id is None
    assert added.scope == "global"


async def mutating_a_returned_model_persists(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        model = await transaction.owned_model(fixture.acme_model.id)
        assert model is not None
        model.upstream_model_id = "gpt-4o"
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        again = await transaction.owned_model(fixture.acme_model.id)

    assert again is not None and again.upstream_model_id == "gpt-4o"


async def deleting_a_model_removes_it(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.globex_scope) as transaction:
        model = await transaction.owned_model(fixture.globex_model.id)
        assert model is not None
        await transaction.delete_model(model)
        await transaction.commit()

    async with fixture.store.begin(fixture.globex_scope) as transaction:
        assert await transaction.model(fixture.globex_model.id) is None


# ---------------------------------------------------------------------------
# references
# ---------------------------------------------------------------------------


async def a_referencing_gateway_is_found(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        found = await transaction.gateways_referencing(fixture.acme_model.id)

    assert [gateway.id for gateway in found] == [fixture.acme_gateway.id]


async def an_unreferenced_model_has_no_gateways(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.gateways_referencing(fixture.global_model.id) == []


async def another_organizations_gateway_is_not_disclosed(fixture: Fixture) -> None:
    """Globex asking what points at Acme's model learns nothing — which is right, and
    also harmless, because SPEC §5.3 forbids their gateway referencing it anyway."""
    async with fixture.store.begin(fixture.globex_scope) as transaction:
        assert await transaction.gateways_referencing(fixture.acme_model.id) == []


async def the_platform_sees_every_referencing_gateway(fixture: Fixture) -> None:
    """The case that matters: a global model can be referenced from anywhere, so the
    superadmin deleting one has to be told about all of them."""
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        found = await transaction.gateways_referencing(fixture.acme_model.id)

    assert [gateway.id for gateway in found] == [fixture.acme_gateway.id]


async def an_unknown_model_is_not_found(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.model(uuid7()) is None
        assert await transaction.owned_model(uuid7()) is None


Check = Callable[[Fixture], Awaitable[None]]

#: Every check, in one list, so neither implementation can be given a shorter exam.
CHECKS: tuple[Check, ...] = (
    an_organization_reads_its_own_model,
    an_organization_reads_the_global_catalog,
    an_organization_cannot_read_anothers_model,
    a_platform_scope_reads_everything,
    listing_is_own_plus_global,
    listing_is_newest_first,
    a_cursor_skips_what_came_before_it,
    the_scope_filter_selects_the_global_catalog,
    the_scope_filter_selects_own_models,
    the_enabled_filter_excludes_disabled_models,
    a_disabled_catalog_model_is_hidden_from_a_tenant,
    the_platform_still_sees_a_disabled_catalog_model,
    a_disabled_model_of_your_own_is_still_visible,
    an_organization_owns_its_own_model,
    an_organization_does_not_own_a_global_model,
    the_platform_owns_a_global_model,
    an_assumed_scope_no_longer_owns_a_global_model,
    a_name_is_taken_within_the_organization,
    another_organizations_name_is_free,
    a_name_can_exclude_the_row_being_edited,
    a_global_name_is_taken_from_any_scope,
    an_org_models_name_does_not_occupy_the_global_namespace,
    adding_a_model_stamps_the_scope,
    adding_a_global_model_owns_it_to_nobody,
    mutating_a_returned_model_persists,
    deleting_a_model_removes_it,
    a_referencing_gateway_is_found,
    an_unreferenced_model_has_no_gateways,
    another_organizations_gateway_is_not_disclosed,
    the_platform_sees_every_referencing_gateway,
    an_unknown_model_is_not_found,
)
