"""The behaviour every gateway store must have, written once.

Same arrangement as ``tests/catalog_store_contract.py``: the in-memory store is only worth
having if it answers like PostgreSQL does, so the questions
:class:`~app.services.gateways.GatewayService` asks are asked here and both
implementations run the same list.

Two organizations, a gateway each, and keys on both — because the checks that matter are
the ones a single-tenant fixture would pass for free. ``api_keys`` carries no
``organization_id``, so "Globex cannot see Acme's key" is a fact about a *join*, and a join
is exactly the kind of thing a memory store gets subtly wrong.

Not a test module itself; it is the shared body the two halves parametrize over.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.ids import uuid7
from app.core.tenancy import TenantScope
from app.db.models import ApiKey, Gateway, Organization, UpstreamModel
from app.services.gateway_store import GatewayStore
from tests.gateway_support import make_gateway_row


@dataclass
class Fixture:
    """Two organizations with a gateway, a model and a key each, plus a global model."""

    store: GatewayStore
    acme: Organization
    globex: Organization
    acme_model: UpstreamModel
    globex_model: UpstreamModel
    global_model: UpstreamModel
    acme_gateway: Gateway
    globex_gateway: Gateway
    acme_key: ApiKey
    globex_key: ApiKey

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
# reading
# ---------------------------------------------------------------------------


async def an_organization_reads_its_own_gateway(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        found = await transaction.gateway(fixture.acme_gateway.id)

    assert found is not None and found.slug == "acme-chat"


async def a_gateway_arrives_with_its_targets_loaded(fixture: Fixture) -> None:
    """The list screen renders the target model's name for every row, so an unloaded
    relationship here is either N queries or, on an async session, an exception."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        found = await transaction.gateway(fixture.acme_gateway.id)

    assert found is not None
    assert [target.upstream_model.name for target in found.targets] == ["acme-gpt"]


async def an_organization_cannot_read_anothers_gateway(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.gateway(fixture.globex_gateway.id) is None


async def a_platform_scope_reads_every_gateway(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.platform_scope) as transaction:
        assert await transaction.gateway(fixture.globex_gateway.id) is not None


async def listing_is_scoped(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        rows = await transaction.gateways(after=None, limit=50)

    assert [gateway.slug for gateway in rows] == ["acme-chat"]


async def listing_is_newest_first(fixture: Fixture) -> None:
    """UUIDv7 ids sort by creation time, which is what makes the cursor just an id."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        await transaction.add_gateway(make_gateway_row(fixture.acme, slug="acme-later"))
        await transaction.commit()
        rows = await transaction.gateways(after=None, limit=50)

    assert [gateway.slug for gateway in rows] == ["acme-later", "acme-chat"]


async def a_cursor_skips_what_came_before_it(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        later = make_gateway_row(fixture.acme, slug="acme-later")
        await transaction.add_gateway(later)
        await transaction.commit()
        rows = await transaction.gateways(after=later.id, limit=50)

    assert [gateway.slug for gateway in rows] == ["acme-chat"]


async def an_unknown_gateway_is_not_found(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.gateway(uuid7()) is None


# ---------------------------------------------------------------------------
# slugs
# ---------------------------------------------------------------------------


async def a_slug_is_taken_across_organizations(fixture: Fixture) -> None:
    """The property the whole endpoint URL depends on: Acme cannot take Globex's slug,
    even though Acme cannot see the gateway holding it."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.slug_taken("globex-chat") is True


async def an_unused_slug_is_free(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.slug_taken("nobody-has-this") is False


# ---------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------


async def a_gateway_may_point_at_its_own_model(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.visible_model(fixture.acme_model.id) is not None


async def a_gateway_may_point_at_a_global_model(fixture: Fixture) -> None:
    """SPEC §5.3: global models and own models, and nothing else."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.visible_model(fixture.global_model.id) is not None


async def a_gateway_may_not_point_at_another_organizations_model(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.visible_model(fixture.globex_model.id) is None


async def setting_targets_replaces_the_previous_chain(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        gateway = await transaction.gateway(fixture.acme_gateway.id)
        assert gateway is not None
        model = await transaction.visible_model(fixture.global_model.id)
        assert model is not None
        await transaction.set_targets(gateway, [model])
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        reloaded = await transaction.gateway(fixture.acme_gateway.id)

    assert reloaded is not None
    assert [target.upstream_model_id for target in reloaded.targets] == [fixture.global_model.id]


async def setting_no_targets_detaches_the_gateway(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        gateway = await transaction.gateway(fixture.acme_gateway.id)
        assert gateway is not None
        await transaction.set_targets(gateway, [])
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        reloaded = await transaction.gateway(fixture.acme_gateway.id)

    assert reloaded is not None and list(reloaded.targets) == []


async def a_new_target_carries_its_model(fixture: Fixture) -> None:
    """Assigning ids alone would leave ``upstream_model`` unloaded, and reading it on an
    async session raises rather than querying — which the response does, immediately."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        gateway = await transaction.gateway(fixture.acme_gateway.id)
        assert gateway is not None
        model = await transaction.visible_model(fixture.global_model.id)
        assert model is not None
        await transaction.set_targets(gateway, [model])

        assert [target.upstream_model.name for target in gateway.targets] == ["shared-gpt-4o"]


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------


async def keys_are_listed_for_their_gateway(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        found = await transaction.keys(fixture.acme_gateway.id)

    assert [key.id for key in found] == [fixture.acme_key.id]


async def another_organizations_keys_are_not_listed(fixture: Fixture) -> None:
    """``api_keys`` has no ``organization_id``, so this is entirely about the join."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.keys(fixture.globex_gateway.id) == []


async def a_key_is_read_through_its_gateways_scope(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.key(fixture.acme_key.id) is not None
        assert await transaction.key(fixture.globex_key.id) is None


async def a_revoked_key_is_still_listed(fixture: Fixture) -> None:
    """History, not clutter: hiding it makes "why did this key stop working" unanswerable,
    and task 07's logs still reference the row."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        key = await transaction.key(fixture.acme_key.id)
        assert key is not None
        key.revoked_at = datetime.now(UTC)
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        found = await transaction.keys(fixture.acme_gateway.id)

    assert [key.id for key in found] == [fixture.acme_key.id]


async def key_counts_exclude_revoked_keys(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.key_counts([fixture.acme_gateway.id]) == {
            fixture.acme_gateway.id: 1
        }

        key = await transaction.key(fixture.acme_key.id)
        assert key is not None
        key.revoked_at = datetime.now(UTC)
        await transaction.commit()

        assert await transaction.key_counts([fixture.acme_gateway.id]) == {}


async def adding_a_key_makes_it_findable(fixture: Fixture) -> None:
    from tests.gateway_support import make_key_row

    key, _ = make_key_row(fixture.acme_gateway.id, name="second")
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        await transaction.add_key(key)
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.key(key.id) is not None


async def an_unknown_key_is_not_found(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.key(uuid7()) is None


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


async def adding_a_gateway_stamps_the_scope(fixture: Fixture) -> None:
    gateway = make_gateway_row(fixture.globex, slug="stamped")
    # Deliberately built with the *wrong* owner: the scope has to win, not the argument.
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        await transaction.add_gateway(gateway)
        await transaction.commit()

    assert gateway.organization_id == fixture.acme.id


async def mutating_a_returned_gateway_persists(fixture: Fixture) -> None:
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        gateway = await transaction.gateway(fixture.acme_gateway.id)
        assert gateway is not None
        gateway.name = "Renamed"
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        reloaded = await transaction.gateway(fixture.acme_gateway.id)

    assert reloaded is not None and reloaded.name == "Renamed"


async def deleting_a_gateway_takes_its_keys(fixture: Fixture) -> None:
    """``ON DELETE CASCADE`` in the schema. A key with no gateway could authenticate
    nothing, so leaving one would only make the revocation list longer."""
    async with fixture.store.begin(fixture.acme_scope) as transaction:
        gateway = await transaction.gateway(fixture.acme_gateway.id)
        assert gateway is not None
        await transaction.delete_gateway(gateway)
        await transaction.commit()

    async with fixture.store.begin(fixture.acme_scope) as transaction:
        assert await transaction.gateway(fixture.acme_gateway.id) is None
        assert await transaction.key(fixture.acme_key.id) is None


Check = Callable[[Fixture], Awaitable[None]]

#: Every check, in one list, so neither implementation can be given a shorter exam.
CHECKS: tuple[Check, ...] = (
    an_organization_reads_its_own_gateway,
    a_gateway_arrives_with_its_targets_loaded,
    an_organization_cannot_read_anothers_gateway,
    a_platform_scope_reads_every_gateway,
    listing_is_scoped,
    listing_is_newest_first,
    a_cursor_skips_what_came_before_it,
    an_unknown_gateway_is_not_found,
    a_slug_is_taken_across_organizations,
    an_unused_slug_is_free,
    a_gateway_may_point_at_its_own_model,
    a_gateway_may_point_at_a_global_model,
    a_gateway_may_not_point_at_another_organizations_model,
    setting_targets_replaces_the_previous_chain,
    setting_no_targets_detaches_the_gateway,
    a_new_target_carries_its_model,
    keys_are_listed_for_their_gateway,
    another_organizations_keys_are_not_listed,
    a_key_is_read_through_its_gateways_scope,
    a_revoked_key_is_still_listed,
    key_counts_exclude_revoked_keys,
    adding_a_key_makes_it_findable,
    an_unknown_key_is_not_found,
    adding_a_gateway_stamps_the_scope,
    mutating_a_returned_gateway_persists,
    deleting_a_gateway_takes_its_keys,
)
