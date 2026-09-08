"""The catalog rules, against the two-organization world.

Everything here runs in memory. What is being tested is policy — who may write the global
catalog, what a ``PATCH`` without a credential means, which gateways block a delete — and
policy does not become more true for having gone through PostgreSQL. The store contract
is what keeps the two agreeing about state.
"""

from __future__ import annotations

import pytest

from app.core.errors import Conflict, Forbidden, NotFound, Validation
from app.core.ids import uuid7
from app.services.catalog import UNSET, ModelDraft, ModelPatch
from tests.catalog_support import ACME_SECRET, PLATFORM_SECRET, make_target_row
from tests.directory_support import World, build_world
from tests.gateway_support import make_gateway_row


@pytest.fixture
def world() -> World:
    return build_world()


def draft(**overrides: object) -> ModelDraft:
    values: dict[str, object] = {
        "name": "new-model",
        "base_url": "https://api.example.com/v1",
        "upstream_model_id": "gpt-4o-mini",
        "credential": "sk-brand-new-key-value",
    }
    values.update(overrides)
    return ModelDraft(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# visibility
# ---------------------------------------------------------------------------


async def test_an_org_user_sees_their_own_models_and_the_global_catalog(world: World) -> None:
    page = await world.catalog.list_models(world.actor(world.acme_admin))

    assert {view.model.name for view in page.items} == {"acme-gpt", "shared-gpt-4o"}


async def test_a_global_model_is_not_editable_by_an_org_user(world: World) -> None:
    page = await world.catalog.list_models(world.actor(world.acme_admin))
    by_name = {view.model.name: view for view in page.items}

    assert by_name["acme-gpt"].editable is True
    assert by_name["shared-gpt-4o"].editable is False


async def test_a_superadmin_sees_every_organizations_models(world: World) -> None:
    page = await world.catalog.list_models(world.actor(world.superadmin))

    assert {view.model.name for view in page.items} == {
        "acme-gpt",
        "globex-gpt",
        "shared-gpt-4o",
    }


async def test_opening_an_organization_narrows_the_list(world: World) -> None:
    page = await world.catalog.list_models(world.platform_actor_assuming(world.globex.id))

    assert {view.model.name for view in page.items} == {"globex-gpt", "shared-gpt-4o"}


async def test_the_scope_filter_is_validated(world: World) -> None:
    with pytest.raises(Validation):
        await world.catalog.list_models(world.actor(world.acme_admin), scope_filter="everything")


async def test_a_viewer_can_read_the_catalog(world: World) -> None:
    """Reading is ``org:read``; the route, not the service, is what refuses a write."""
    page = await world.catalog.list_models(world.actor(world.acme_viewer))

    assert page.items


# ---------------------------------------------------------------------------
# the credential is write-only
# ---------------------------------------------------------------------------


async def test_a_stored_credential_is_never_on_a_view(world: World) -> None:
    view = await world.catalog.get_model(world.actor(world.acme_admin), world.acme_model.id)

    assert ACME_SECRET not in repr(view)
    assert view.credential_hint == "sk-...pear"


async def test_an_org_user_cannot_read_a_global_models_hint(world: World) -> None:
    view = await world.catalog.get_model(world.actor(world.acme_admin), world.global_model.id)

    assert view.credential_hint is None


async def test_a_superadmin_can_read_a_global_models_hint(world: World) -> None:
    view = await world.catalog.get_model(world.actor(world.superadmin), world.global_model.id)

    assert view.credential_hint is not None


async def test_a_global_models_extra_headers_are_withheld_from_a_tenant(world: World) -> None:
    """``extra_headers`` is applied last and can therefore contain an auth header, which
    is exactly what the credential field is protecting."""
    view = await world.catalog.get_model(world.actor(world.acme_admin), world.global_model.id)

    assert view.extra_headers == {}
    assert "operator-only-value" not in repr(view)


async def test_creating_stores_the_hint_from_the_plaintext(world: World) -> None:
    view = await world.catalog.create_model(
        world.actor(world.acme_admin), draft(credential="sk-1234567890abcdef")
    )

    assert view.model.credential_hint == "sk-...cdef"
    assert view.model.credential_ciphertext is not None


async def test_a_stored_credential_round_trips(world: World) -> None:
    """Encrypt on write, decrypt for use, and never in between."""
    view = await world.catalog.create_model(
        world.actor(world.acme_admin), draft(credential="sk-round-trip-value")
    )

    await world.catalog.test_model(world.actor(world.acme_admin), view.model.id)

    assert world.probe.last.credential == "sk-round-trip-value"


async def test_a_short_credential_is_masked_entirely(world: World) -> None:
    view = await world.catalog.create_model(world.actor(world.acme_admin), draft(credential="tiny"))

    assert view.model.credential_hint == "..."


# ---------------------------------------------------------------------------
# creating
# ---------------------------------------------------------------------------


async def test_creating_stamps_the_organization(world: World) -> None:
    view = await world.catalog.create_model(world.actor(world.acme_admin), draft())

    assert view.model.organization_id == world.acme.id
    assert view.model.scope == "org"


async def test_an_org_user_cannot_create_a_global_model(world: World) -> None:
    with pytest.raises(Forbidden):
        await world.catalog.create_model(world.actor(world.acme_admin), draft(scope="global"))


async def test_a_superadmin_creates_a_global_model(world: World) -> None:
    view = await world.catalog.create_model(world.actor(world.superadmin), draft(scope="global"))

    assert view.model.organization_id is None
    assert view.model.scope == "global"


async def test_a_superadmin_at_platform_scope_cannot_create_an_org_model(world: World) -> None:
    """There is no organization to stamp on it. Opening one first is the answer, and it
    is the same answer the directory gives for inviting a member."""
    with pytest.raises(Forbidden):
        await world.catalog.create_model(world.actor(world.superadmin), draft())


async def test_a_superadmin_creates_an_org_model_after_opening_it(world: World) -> None:
    view = await world.catalog.create_model(world.platform_actor_assuming(world.globex.id), draft())

    assert view.model.organization_id == world.globex.id


async def test_a_duplicate_name_in_the_organization_is_refused(world: World) -> None:
    with pytest.raises(Conflict) as failure:
        await world.catalog.create_model(world.actor(world.acme_admin), draft(name="acme-gpt"))

    assert "this organization" in str(failure.value)


async def test_the_same_name_in_another_organization_is_fine(world: World) -> None:
    view = await world.catalog.create_model(world.actor(world.globex_admin), draft(name="acme-gpt"))

    assert view.model.name == "acme-gpt"


async def test_a_duplicate_global_name_is_refused(world: World) -> None:
    with pytest.raises(Conflict) as failure:
        await world.catalog.create_model(
            world.actor(world.superadmin), draft(name="shared-gpt-4o", scope="global")
        )

    assert "global catalog" in str(failure.value)


async def test_an_org_model_may_share_a_name_with_a_global_one(world: World) -> None:
    """Two namespaces, and the constraint is per namespace. An organization overriding
    the shared model with one of their own is a reasonable thing to want."""
    view = await world.catalog.create_model(
        world.actor(world.acme_admin), draft(name="shared-gpt-4o")
    )

    assert view.model.name == "shared-gpt-4o"


async def test_a_dialect_with_no_adapter_is_refused_with_a_reason(world: World) -> None:
    """A dialect the column would accept but this build cannot serve is refused here,
    naming the ones it can, rather than failing at request time on somebody's traffic.

    ``bedrock`` stands in for whichever dialect is next: the schema layer checks membership
    of the column's allowlist and this checks that something is registered to serve it, and
    the two move at different times — ``anthropic`` spent a release in exactly this state.
    """
    with pytest.raises(Validation) as failure:
        await world.catalog.create_model(world.actor(world.acme_admin), draft(dialect="bedrock"))

    assert "not yet supported" in str(failure.value)
    assert failure.value.param == "dialect"


async def test_the_anthropic_dialect_is_now_servable(world: World) -> None:
    """Task 16's registration, from the write path's point of view. The check above did
    not change; the registry it reads did."""
    view = await world.catalog.create_model(
        world.actor(world.acme_admin), draft(name="claude", dialect="anthropic")
    )

    assert view.model.dialect == "anthropic"


async def test_no_auth_with_a_credential_is_refused(world: World) -> None:
    with pytest.raises(Validation) as failure:
        await world.catalog.create_model(
            world.actor(world.acme_admin), draft(auth_type="none", credential="sk-still-here")
        )

    assert failure.value.param == "auth_type"


async def test_no_auth_without_a_credential_is_fine(world: World) -> None:
    view = await world.catalog.create_model(
        world.actor(world.acme_admin), draft(auth_type="none", credential=None)
    )

    assert view.model.auth_type == "none"


async def test_an_unknown_default_param_is_refused(world: World) -> None:
    with pytest.raises(Validation) as failure:
        await world.catalog.create_model(
            world.actor(world.acme_admin), draft(default_params={"temprature": 0.5})
        )

    assert "temprature" in str(failure.value)


async def test_a_header_the_transport_owns_is_refused(world: World) -> None:
    with pytest.raises(Validation) as failure:
        await world.catalog.create_model(
            world.actor(world.acme_admin), draft(extra_headers={"Content-Length": "12"})
        )

    assert failure.value.param == "extra_headers"


# ---------------------------------------------------------------------------
# updating
# ---------------------------------------------------------------------------


async def test_an_omitted_credential_is_kept(world: World) -> None:
    before = world.acme_model.credential_ciphertext

    view = await world.catalog.update_model(
        world.actor(world.acme_admin), world.acme_model.id, ModelPatch(name="renamed")
    )

    assert view.model.credential_ciphertext == before
    assert view.model.credential_hint is not None


async def test_an_explicit_null_credential_clears_it(world: World) -> None:
    view = await world.catalog.update_model(
        world.actor(world.acme_admin),
        world.acme_model.id,
        ModelPatch(credential=None, auth_type="none"),
    )

    assert view.model.credential_ciphertext is None
    assert view.model.credential_hint is None


async def test_a_new_credential_replaces_the_hint(world: World) -> None:
    view = await world.catalog.update_model(
        world.actor(world.acme_admin),
        world.acme_model.id,
        ModelPatch(credential="sk-rotated-0000wxyz"),
    )

    assert view.model.credential_hint == "sk-...wxyz"


async def test_clearing_a_credential_while_auth_is_bearer_is_allowed(world: World) -> None:
    """A model can legitimately exist with no key yet — that is what "Test connection"
    is for. Only the contradictory combination is refused."""
    view = await world.catalog.update_model(
        world.actor(world.acme_admin), world.acme_model.id, ModelPatch(credential=None)
    )

    assert view.model.credential_ciphertext is None


async def test_keeping_a_credential_while_switching_to_no_auth_is_refused(world: World) -> None:
    with pytest.raises(Validation) as failure:
        await world.catalog.update_model(
            world.actor(world.acme_admin), world.acme_model.id, ModelPatch(auth_type="none")
        )

    assert failure.value.param == "auth_type"


async def test_an_org_user_cannot_edit_a_global_model(world: World) -> None:
    with pytest.raises(NotFound):
        await world.catalog.update_model(
            world.actor(world.acme_admin), world.global_model.id, ModelPatch(name="taken over")
        )


async def test_a_superadmin_can_edit_a_global_model(world: World) -> None:
    view = await world.catalog.update_model(
        world.actor(world.superadmin), world.global_model.id, ModelPatch(name="renamed-shared")
    )

    assert view.model.name == "renamed-shared"


async def test_editing_another_organizations_model_is_not_found(world: World) -> None:
    with pytest.raises(NotFound):
        await world.catalog.update_model(
            world.actor(world.acme_admin), world.globex_model.id, ModelPatch(enabled=False)
        )


async def test_renaming_onto_a_taken_name_is_refused(world: World) -> None:
    await world.catalog.create_model(world.actor(world.acme_admin), draft(name="second"))

    with pytest.raises(Conflict):
        await world.catalog.update_model(
            world.actor(world.acme_admin), world.acme_model.id, ModelPatch(name="second")
        )


async def test_renaming_to_its_own_name_is_not_a_conflict(world: World) -> None:
    view = await world.catalog.update_model(
        world.actor(world.acme_admin), world.acme_model.id, ModelPatch(name="acme-gpt")
    )

    assert view.model.name == "acme-gpt"


async def test_an_unset_field_is_left_alone(world: World) -> None:
    before = world.acme_model.base_url

    view = await world.catalog.update_model(
        world.actor(world.acme_admin), world.acme_model.id, ModelPatch(base_url=UNSET)
    )

    assert view.model.base_url == before


async def test_updating_validates_default_params(world: World) -> None:
    with pytest.raises(Validation):
        await world.catalog.update_model(
            world.actor(world.acme_admin),
            world.acme_model.id,
            ModelPatch(default_params={"temperature": 9}),
        )


# ---------------------------------------------------------------------------
# deleting
# ---------------------------------------------------------------------------


async def test_deleting_a_referenced_model_names_the_gateways(world: World) -> None:
    with pytest.raises(Conflict) as failure:
        await world.catalog.delete_model(world.actor(world.acme_admin), world.acme_model.id)

    assert "Acme Chat" in failure.value.message
    assert failure.value.details["gateways"][0]["slug"] == "acme-chat"


async def test_disabling_a_referenced_model_is_allowed(world: World) -> None:
    """The escape hatch the refusal points at: switching it off takes effect on the next
    request, without touching the gateway."""
    view = await world.catalog.update_model(
        world.actor(world.acme_admin), world.acme_model.id, ModelPatch(enabled=False)
    )

    assert view.model.enabled is False


async def test_deleting_an_unreferenced_model_works(world: World) -> None:
    created = await world.catalog.create_model(world.actor(world.acme_admin), draft())

    await world.catalog.delete_model(world.actor(world.acme_admin), created.model.id)

    with pytest.raises(NotFound):
        await world.catalog.get_model(world.actor(world.acme_admin), created.model.id)


async def test_a_second_gateway_is_named_too(world: World) -> None:
    second = make_gateway_row(world.acme, slug="acme-support")
    world.database.add_gateway(second)
    world.database.add_target(make_target_row(second.id, world.acme_model.id))

    with pytest.raises(Conflict) as failure:
        await world.catalog.delete_model(world.actor(world.acme_admin), world.acme_model.id)

    assert len(failure.value.details["gateways"]) == 2
    assert "gateways" in failure.value.message


async def test_an_org_user_cannot_delete_a_global_model(world: World) -> None:
    with pytest.raises(NotFound):
        await world.catalog.delete_model(world.actor(world.acme_admin), world.global_model.id)


async def test_deleting_something_that_does_not_exist_is_not_found(world: World) -> None:
    with pytest.raises(NotFound):
        await world.catalog.delete_model(world.actor(world.acme_admin), uuid7())


# ---------------------------------------------------------------------------
# test connection
# ---------------------------------------------------------------------------


async def test_testing_a_model_sends_its_stored_configuration(world: World) -> None:
    result = await world.catalog.test_model(world.actor(world.acme_admin), world.acme_model.id)

    assert result.ok is True
    assert world.probe.last.base_url == world.acme_model.base_url
    assert world.probe.last.credential == ACME_SECRET


async def test_testing_a_draft_uses_the_supplied_credential(world: World) -> None:
    await world.catalog.test_draft(
        world.actor(world.acme_admin), draft(credential="sk-typed-into-the-form")
    )

    assert world.probe.last.credential == "sk-typed-into-the-form"


async def test_a_draft_probe_carries_no_prompt_layers(world: World) -> None:
    """A probe answers "can I reach it", not "is my prompt right". Sending the system
    context would make the cost depend on how much text somebody pasted into it."""
    await world.catalog.test_draft(world.actor(world.acme_admin), draft(system_context="a" * 5000))

    assert world.probe.last.system_context is None


async def test_an_org_user_cannot_test_a_global_model(world: World) -> None:
    """A probe spends the owner's tokens, and the owner of a global model is the
    platform. Same 404 as every other write on it."""
    with pytest.raises(NotFound):
        await world.catalog.test_model(world.actor(world.acme_admin), world.global_model.id)


async def test_a_superadmin_can_test_a_global_model(world: World) -> None:
    result = await world.catalog.test_model(world.actor(world.superadmin), world.global_model.id)

    assert result.ok is True
    assert world.probe.last.credential == PLATFORM_SECRET


async def test_testing_another_organizations_model_is_not_found(world: World) -> None:
    with pytest.raises(NotFound):
        await world.catalog.test_model(world.actor(world.acme_admin), world.globex_model.id)


async def test_an_undecryptable_credential_says_so(world: World) -> None:
    """A master key that no longer matches. Reporting it beats probing without auth and
    relaying the provider's 401, which sends the operator after the wrong bug."""
    world.acme_model.credential_ciphertext = b"\x01" + b"\x00" * 200

    with pytest.raises(Conflict) as failure:
        await world.catalog.test_model(world.actor(world.acme_admin), world.acme_model.id)

    assert "encryption key" in str(failure.value)
