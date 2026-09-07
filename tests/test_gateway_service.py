"""The gateway rules, against the two-organization world.

Everything here runs in memory. What is being tested is policy — which slugs are refused,
what a ``PATCH`` may not change, when a key stops working, what invalidates the config
cache — and policy does not become more true for having gone through PostgreSQL. The store
contract is what keeps the two agreeing about state.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import Conflict, NotFound, Validation
from app.core.ids import uuid7
from app.schemas.gateway_config import LoggingConfig, MemoryConfig
from app.services.catalog import ModelPatch
from app.services.gateways import (
    MAX_KEYS_PER_GATEWAY,
    MAX_TARGETS,
    RESERVED_SLUGS,
    GatewayDraft,
    GatewayPatch,
    GatewayView,
    TargetSpec,
)
from tests.directory_support import World, build_world


@pytest.fixture
def world() -> World:
    return build_world()


def draft(**overrides: object) -> GatewayDraft:
    values: dict[str, object] = {"name": "Support Bot", "slug": "acme-support"}
    # `model_id=` is kept as a shorthand here for the same reason the API keeps it: most
    # of these tests are about something other than routing, and one target is the
    # uninteresting case they want.
    model_id = overrides.pop("model_id", None)
    if model_id is not None:
        values["targets"] = (TargetSpec(model_id=model_id),)  # type: ignore[arg-type]
    values.update(overrides)
    return GatewayDraft(**values)  # type: ignore[arg-type]


def chain(*pairs: tuple[uuid.UUID, int]) -> tuple[TargetSpec, ...]:
    return tuple(TargetSpec(model_id=model_id, weight=weight) for model_id, weight in pairs)


def names(view: GatewayView) -> list[str]:
    return [target.model.name for target in view.targets]


# ---------------------------------------------------------------------------
# slugs
# ---------------------------------------------------------------------------


async def test_a_gateway_is_created_with_its_endpoint_url(world: World) -> None:
    view = await world.gateways.create_gateway(world.actor(world.acme_admin), draft())

    assert view.gateway.slug == "acme-support"
    assert view.endpoint_url.endswith("/g/acme-support/v1")


async def test_the_endpoint_url_comes_from_the_server_not_the_browser(world: World) -> None:
    """Behind an ingress the browser's origin is not the public one, and the URL next to
    a copy button has to be the one that works from outside."""
    view = await world.gateways.create_gateway(world.actor(world.acme_admin), draft())

    assert view.endpoint_url.startswith(world.settings.public_base_url)


async def test_a_slug_is_lower_cased_and_trimmed(world: World) -> None:
    view = await world.gateways.create_gateway(
        world.actor(world.acme_admin), draft(slug="  ACME-Support  ")
    )

    assert view.gateway.slug == "acme-support"


@pytest.mark.parametrize("slug", ["ab", "-leading", "trailing-", "has space", "under_score"])
async def test_a_malformed_slug_is_refused(slug: str, world: World) -> None:
    with pytest.raises(Validation) as raised:
        await world.gateways.create_gateway(world.actor(world.acme_admin), draft(slug=slug))

    assert raised.value.param == "slug"


@pytest.mark.parametrize("slug", sorted(RESERVED_SLUGS))
async def test_a_reserved_slug_is_refused(slug: str, world: World) -> None:
    with pytest.raises(Validation) as raised:
        await world.gateways.create_gateway(world.actor(world.acme_admin), draft(slug=slug))

    assert "reserved" in raised.value.message


async def test_a_slug_taken_by_another_organization_is_refused(world: World) -> None:
    """Globally unique, because it is in a public URL. The message says the slug is
    taken and nothing about who holds it."""
    with pytest.raises(Conflict) as raised:
        await world.gateways.create_gateway(
            world.actor(world.acme_admin), draft(slug="globex-chat")
        )

    assert raised.value.param == "slug"
    assert "globex" not in raised.value.message.lower().replace("globex-chat", "")


async def test_a_slug_cannot_be_changed(world: World) -> None:
    """There is no field on :class:`GatewayPatch` to change it with — a compile-time
    refusal rather than a runtime one. The API layer turns an attempt into a 422 that
    explains why; this is the reason it can."""
    assert not hasattr(GatewayPatch(), "slug")


# ---------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------


async def test_a_gateway_can_point_at_its_own_model(world: World) -> None:
    view = await world.gateways.create_gateway(
        world.actor(world.acme_admin), draft(model_id=world.acme_model.id)
    )

    assert names(view) == ["acme-gpt"]


async def test_a_gateway_can_point_at_a_global_model(world: World) -> None:
    view = await world.gateways.create_gateway(
        world.actor(world.acme_admin), draft(model_id=world.global_model.id)
    )

    assert names(view) == ["shared-gpt-4o"]


async def test_a_gateway_cannot_point_at_another_organizations_model(world: World) -> None:
    """A 422 naming the field rather than a 404: the id came from a picker, and the
    message belongs on that picker."""
    with pytest.raises(Validation) as raised:
        await world.gateways.create_gateway(
            world.actor(world.acme_admin), draft(model_id=world.globex_model.id)
        )

    assert raised.value.param == "targets.0.model_id"


async def test_a_gateway_can_be_created_before_any_model_exists(world: World) -> None:
    view = await world.gateways.create_gateway(world.actor(world.acme_admin), draft())

    assert view.targets == ()


async def test_an_empty_chain_detaches_the_gateway(world: World) -> None:
    """Distinct from omitting it. Parking an endpoint without deleting it is a real
    thing to want, and an empty list is how the patch says so."""
    actor = world.actor(world.acme_admin)
    view = await world.gateways.update_gateway(
        actor, world.acme_gateway.id, GatewayPatch(targets=())
    )

    assert view.targets == ()


async def test_omitting_the_model_leaves_the_target_alone(world: World) -> None:
    actor = world.actor(world.acme_admin)
    view = await world.gateways.update_gateway(
        actor, world.acme_gateway.id, GatewayPatch(name="Renamed")
    )

    assert names(view) == ["acme-gpt"]


# ---------------------------------------------------------------------------
# routing mode
# ---------------------------------------------------------------------------


async def test_an_unknown_routing_mode_is_refused(world: World) -> None:
    with pytest.raises(Validation) as raised:
        await world.gateways.create_gateway(
            world.actor(world.acme_admin), draft(routing_mode="round_robin")
        )

    assert raised.value.param == "routing_mode"


async def test_a_failover_chain_is_saved_in_priority_order(world: World) -> None:
    view = await world.gateways.create_gateway(
        world.actor(world.acme_admin),
        draft(
            routing_mode="failover",
            targets=chain((world.acme_model.id, 100), (world.global_model.id, 100)),
        ),
    )

    assert names(view) == ["acme-gpt", "shared-gpt-4o"]
    assert [target.priority for target in view.targets] == [0, 1]


async def test_a_failover_chain_needs_somewhere_to_fail_over_to(world: World) -> None:
    """One target in failover mode is a gateway that claims a property it does not have.
    Refusing at save time is the only place the message can name the fix."""
    with pytest.raises(Validation) as raised:
        await world.gateways.create_gateway(
            world.actor(world.acme_admin),
            draft(routing_mode="failover", targets=chain((world.acme_model.id, 100))),
        )

    assert raised.value.param == "targets"
    assert "fail over to" in raised.value.message


async def test_single_mode_takes_one_target(world: World) -> None:
    with pytest.raises(Validation) as raised:
        await world.gateways.create_gateway(
            world.actor(world.acme_admin),
            draft(targets=chain((world.acme_model.id, 100), (world.global_model.id, 100))),
        )

    assert raised.value.param == "targets"


async def test_ab_weights_must_add_up_to_a_hundred(world: World) -> None:
    with pytest.raises(Validation) as raised:
        await world.gateways.create_gateway(
            world.actor(world.acme_admin),
            draft(
                routing_mode="ab_split",
                targets=chain((world.acme_model.id, 70), (world.global_model.id, 20)),
            ),
        )

    assert raised.value.param == "targets"
    assert "90" in raised.value.message


async def test_ab_weights_are_never_normalised_on_the_callers_behalf(world: World) -> None:
    """The weights are somebody's experiment. Rescaling 70/20 to 78/22 would change what
    is being measured and tell nobody, so the save is refused instead."""
    actor = world.actor(world.acme_admin)
    with pytest.raises(Validation):
        await world.gateways.create_gateway(
            actor,
            draft(
                routing_mode="ab_split",
                targets=chain((world.acme_model.id, 70), (world.global_model.id, 20)),
            ),
        )

    view = await world.gateways.create_gateway(
        actor,
        draft(
            routing_mode="ab_split",
            targets=chain((world.acme_model.id, 70), (world.global_model.id, 30)),
        ),
    )

    assert [target.weight for target in view.targets] == [70, 30]


async def test_the_same_model_cannot_appear_twice(world: World) -> None:
    """A duplicate in a failover chain retries the upstream that just failed."""
    with pytest.raises(Validation) as raised:
        await world.gateways.create_gateway(
            world.actor(world.acme_admin),
            draft(
                routing_mode="failover",
                targets=chain((world.acme_model.id, 100), (world.acme_model.id, 100)),
            ),
        )

    assert raised.value.param == "targets"
    assert "twice" in raised.value.message


async def test_switching_mode_alone_is_validated_against_the_existing_chain(
    world: World,
) -> None:
    """Otherwise "switch to A/B" saves happily on a one-target gateway and starts
    splitting 100/0 while the screen says it is running an experiment."""
    with pytest.raises(Validation) as raised:
        await world.gateways.update_gateway(
            world.actor(world.acme_admin),
            world.acme_gateway.id,
            GatewayPatch(routing_mode="ab_split"),
        )

    assert raised.value.param == "targets"


async def test_a_gateway_with_no_targets_may_take_any_mode(world: World) -> None:
    """A gateway can exist before its models do; the endpoint answers 503 and says so.
    Refusing the mode as well would make the editor unusable in the order people use it."""
    view = await world.gateways.create_gateway(
        world.actor(world.acme_admin), draft(routing_mode="ab_split")
    )

    assert view.gateway.routing_mode == "ab_split"
    assert view.targets == ()


async def test_a_chain_is_capped(world: World) -> None:
    with pytest.raises(Validation) as raised:
        await world.gateways.create_gateway(
            world.actor(world.acme_admin),
            draft(
                routing_mode="failover",
                targets=tuple(TargetSpec(model_id=uuid7()) for _ in range(MAX_TARGETS + 1)),
            ),
        )

    assert raised.value.param == "targets"


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------


async def test_param_overrides_are_validated_against_the_allowlist(world: World) -> None:
    with pytest.raises(Validation) as raised:
        await world.gateways.create_gateway(
            world.actor(world.acme_admin), draft(param_overrides={"temprature": 0.2})
        )

    assert raised.value.param == "param_overrides.temprature"


async def test_locked_params_are_validated_the_same_way(world: World) -> None:
    """Same allowlist, different field — so the message lands on the input the user is
    actually looking at."""
    with pytest.raises(Validation) as raised:
        await world.gateways.create_gateway(
            world.actor(world.acme_admin), draft(locked_params={"temperature": 4})
        )

    assert raised.value.param == "locked_params.temperature"


async def test_locked_params_are_stored_separately_from_overrides(world: World) -> None:
    view = await world.gateways.create_gateway(
        world.actor(world.acme_admin),
        draft(param_overrides={"top_p": 0.9}, locked_params={"temperature": 0.2}),
    )

    assert view.gateway.param_overrides == {"top_p": 0.9}
    assert view.gateway.locked_params == {"temperature": 0.2}


# ---------------------------------------------------------------------------
# config blobs
# ---------------------------------------------------------------------------


async def test_a_new_gateway_stores_complete_defaults(world: World) -> None:
    """The stored blob is filled in on write, so the API always shows what will happen
    rather than an empty object the reader has to know the defaults for."""
    view = await world.gateways.create_gateway(world.actor(world.acme_admin), draft())

    assert view.gateway.memory_config["doc_top_k"] == MemoryConfig().doc_top_k
    assert view.gateway.logging_config["retention_days"] == LoggingConfig().retention_days
    assert view.gateway.limits["requests_per_minute"] is None


async def test_a_config_patch_merges_rather_than_replaces(world: World) -> None:
    """A form that owns one section must not wipe the settings of another."""
    actor = world.actor(world.acme_admin)
    await world.gateways.update_gateway(
        actor, world.acme_gateway.id, GatewayPatch(memory_config={"doc_top_k": 12})
    )
    view = await world.gateways.update_gateway(
        actor, world.acme_gateway.id, GatewayPatch(memory_config={"memory_top_k": 3})
    )

    assert view.gateway.memory_config["doc_top_k"] == 12
    assert view.gateway.memory_config["memory_top_k"] == 3


async def test_an_unknown_config_key_is_refused(world: World) -> None:
    with pytest.raises(Validation) as raised:
        await world.gateways.update_gateway(
            world.actor(world.acme_admin),
            world.acme_gateway.id,
            GatewayPatch(memory_config={"doc_top_kk": 8}),
        )

    assert raised.value.param == "memory_config.doc_top_kk"


async def test_a_config_value_out_of_range_names_its_section(world: World) -> None:
    with pytest.raises(Validation) as raised:
        await world.gateways.update_gateway(
            world.actor(world.acme_admin),
            world.acme_gateway.id,
            GatewayPatch(limits={"requests_per_minute": 0}),
        )

    assert raised.value.param == "limits.requests_per_minute"


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------


async def test_another_organizations_gateway_is_not_found(world: World) -> None:
    with pytest.raises(NotFound):
        await world.gateways.get_gateway(world.actor(world.acme_admin), world.globex_gateway.id)


async def test_a_gateway_that_does_not_exist_answers_identically(world: World) -> None:
    with pytest.raises(NotFound):
        await world.gateways.get_gateway(world.actor(world.acme_admin), uuid7())


async def test_a_list_never_includes_another_organization(world: World) -> None:
    page = await world.gateways.list_gateways(world.actor(world.acme_admin))

    assert [view.gateway.slug for view in page.items] == ["acme-chat"]


async def test_a_superadmin_sees_every_gateway(world: World) -> None:
    page = await world.gateways.list_gateways(world.actor(world.superadmin))

    assert {view.gateway.slug for view in page.items} == {"acme-chat", "globex-chat"}


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------


async def test_a_key_is_returned_once_in_plaintext(world: World) -> None:
    issued = await world.gateways.create_key(
        world.actor(world.acme_admin), world.acme_gateway.id, name="production"
    )

    assert issued.token.startswith("mg_")
    # The row keeps a hash and a prefix, and neither can produce the token again.
    assert issued.token not in (issued.key.key_hash, issued.key.prefix)


async def test_the_token_embeds_the_row_id(world: World) -> None:
    """What makes authentication one primary-key lookup instead of a scan."""
    from app.core import keys

    issued = await world.gateways.create_key(
        world.actor(world.acme_admin), world.acme_gateway.id, name="production"
    )
    parsed = keys.parse(issued.token)

    assert parsed is not None and parsed.key_id == issued.key.id


async def test_a_key_cannot_be_read_back(world: World) -> None:
    """There is no method on the service that returns one, and the listing carries only
    the prefix — which is the durable display form and holds no secret."""
    issued = await world.gateways.create_key(
        world.actor(world.acme_admin), world.acme_gateway.id, name="production"
    )
    listed = await world.gateways.list_keys(world.actor(world.acme_admin), world.acme_gateway.id)

    assert issued.token not in {key.prefix for key in listed}
    assert not hasattr(world.gateways, "reveal_key")


async def test_a_key_needs_a_name(world: World) -> None:
    with pytest.raises(Validation) as raised:
        await world.gateways.create_key(
            world.actor(world.acme_admin), world.acme_gateway.id, name="   "
        )

    assert raised.value.param == "name"


async def test_an_expiry_in_the_past_is_refused(world: World) -> None:
    with pytest.raises(Validation) as raised:
        await world.gateways.create_key(
            world.actor(world.acme_admin),
            world.acme_gateway.id,
            name="stale",
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )

    assert raised.value.param == "expires_at"


async def test_an_expiry_is_stored(world: World) -> None:
    when = datetime.now(UTC) + timedelta(days=30)
    issued = await world.gateways.create_key(
        world.actor(world.acme_admin), world.acme_gateway.id, name="temporary", expires_at=when
    )

    assert issued.key.expires_at == when


async def test_revoking_a_key_stamps_it_rather_than_deleting_it(world: World) -> None:
    """Task 07's request logs reference the row; a log line that cannot say which key
    made the call is worth less than the row it saved."""
    actor = world.actor(world.acme_admin)
    revoked = await world.gateways.revoke_key(actor, world.acme_key.id)

    assert revoked.revoked_at is not None
    listed = await world.gateways.list_keys(actor, world.acme_gateway.id)
    assert world.acme_key.id in {key.id for key in listed}


async def test_revoking_twice_keeps_the_first_timestamp(world: World) -> None:
    actor = world.actor(world.acme_admin)
    first = await world.gateways.revoke_key(actor, world.acme_key.id)
    stamp = first.revoked_at
    second = await world.gateways.revoke_key(actor, world.acme_key.id)

    assert second.revoked_at == stamp


async def test_another_organizations_key_cannot_be_revoked(world: World) -> None:
    with pytest.raises(NotFound):
        await world.gateways.revoke_key(world.actor(world.acme_admin), world.globex_key.id)


async def test_a_gateway_caps_its_active_keys(world: World) -> None:
    actor = world.actor(world.acme_admin)
    # One already exists in the world, so this fills the rest of the allowance.
    for index in range(MAX_KEYS_PER_GATEWAY - 1):
        await world.gateways.create_key(actor, world.acme_gateway.id, name=f"key-{index}")

    with pytest.raises(Conflict):
        await world.gateways.create_key(actor, world.acme_gateway.id, name="one too many")


async def test_a_revoked_key_frees_up_its_slot(world: World) -> None:
    actor = world.actor(world.acme_admin)
    for index in range(MAX_KEYS_PER_GATEWAY - 1):
        await world.gateways.create_key(actor, world.acme_gateway.id, name=f"key-{index}")
    await world.gateways.revoke_key(actor, world.acme_key.id)

    await world.gateways.create_key(actor, world.acme_gateway.id, name="replacement")


# ---------------------------------------------------------------------------
# the config cache
# ---------------------------------------------------------------------------


async def test_creating_a_gateway_invalidates_its_slug(world: World) -> None:
    await world.gateways.create_gateway(world.actor(world.acme_admin), draft())

    assert "acme-support" in world.cache.invalidated


async def test_editing_a_gateway_invalidates_its_slug(world: World) -> None:
    world.cache.invalidated.clear()
    await world.gateways.update_gateway(
        world.actor(world.acme_admin),
        world.acme_gateway.id,
        GatewayPatch(system_context="Be concise."),
    )

    assert world.cache.invalidated == ["acme-chat"]


async def test_deleting_a_gateway_invalidates_its_slug(world: World) -> None:
    world.cache.invalidated.clear()
    await world.gateways.delete_gateway(world.actor(world.acme_admin), world.acme_gateway.id)

    assert world.cache.invalidated == ["acme-chat"]


async def test_editing_a_model_invalidates_every_gateway_pointing_at_it(world: World) -> None:
    """The cross-service half of the contract, and the reason the two share one cache.

    Rotating a credential has to reach the endpoints using it, or they keep calling the
    provider with the old one until a TTL expires.
    """
    world.cache.invalidated.clear()
    await world.catalog.update_model(
        world.actor(world.acme_admin),
        world.acme_model.id,
        ModelPatch(base_url="https://elsewhere.example.com/v1"),
    )

    assert world.cache.invalidated == ["acme-chat"]


async def test_revoking_a_key_does_not_touch_the_config_cache(world: World) -> None:
    """Not an oversight — the point. Keys are never cached, so revocation takes effect on
    the next request rather than at the end of a TTL, and there is nothing to invalidate.
    """
    world.cache.invalidated.clear()
    await world.gateways.revoke_key(world.actor(world.acme_admin), world.acme_key.id)

    assert world.cache.invalidated == []


# ---------------------------------------------------------------------------
# the probe
# ---------------------------------------------------------------------------


async def test_testing_a_gateway_probes_it_by_slug(world: World) -> None:
    """By slug, so the probe goes through the resolver — the same path, cache included,
    that a customer's request takes."""
    result = await world.gateways.test_gateway(
        world.actor(world.acme_admin), world.acme_gateway.id, message="are you there?"
    )

    assert result.ok is True
    assert world.gateway_probe.last == ("acme-chat", "are you there?")


async def test_another_organizations_gateway_cannot_be_probed(world: World) -> None:
    with pytest.raises(NotFound):
        await world.gateways.test_gateway(
            world.actor(world.acme_admin), world.globex_gateway.id, message="hi"
        )


# ---------------------------------------------------------------------------
# deletion
# ---------------------------------------------------------------------------


async def test_deleting_a_gateway_takes_its_keys(world: World) -> None:
    actor = world.actor(world.acme_admin)
    await world.gateways.delete_gateway(actor, world.acme_gateway.id)

    with pytest.raises(NotFound):
        await world.gateways.revoke_key(actor, world.acme_key.id)


async def test_another_organizations_gateway_cannot_be_deleted(world: World) -> None:
    with pytest.raises(NotFound):
        await world.gateways.delete_gateway(world.actor(world.acme_admin), world.globex_gateway.id)
