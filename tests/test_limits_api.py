"""The Limits screen's endpoints, the ceiling on the way in, and the throttled-user list.

Three concerns in one file because they are one feature from a user's side: what this
gateway's caps are, what the platform will let them be, and who has been hitting them.
"""

from __future__ import annotations

import math
import time
import uuid
from datetime import timedelta

import pytest

from app.core.config import Settings, get_settings
from app.core.errors import Validation
from app.services.gateways import GatewayDraft, GatewayPatch, TargetSpec
from app.services.limits import bucket_key
from tests.conftest import DirectoryHarness
from tests.directory_support import World, build_world
from tests.monitoring_support import THROTTLED_AT, make_end_user, make_log_row


def with_ceilings(**caps: int) -> Settings:
    return get_settings().model_copy(
        update={f"global_model_{name}": value for name, value in caps.items()}
    )


def spend(world: World, limit: str, amount: float) -> None:
    """Put some usage in the gateway's bucket, the way a burst of traffic would."""
    key = bucket_key(limit, gateway_id=world.acme_gateway.id)
    slot = math.floor(time.time() / 60)
    world.auth.limit_buckets.counters[key] = {slot: amount}


# ---------------------------------------------------------------------------
# reading one gateway's limits
# ---------------------------------------------------------------------------


async def test_the_limits_of_a_gateway_are_readable(directory: DirectoryHarness) -> None:
    world = directory.world
    response = await directory.as_user(
        world.acme_admin, "GET", f"/api/v1/gateways/{world.acme_gateway.id}/limits"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["gateway_id"] == str(world.acme_gateway.id)
    assert body["configured"]["requests_per_minute"] is None


async def test_a_viewer_may_read_them(directory: DirectoryHarness) -> None:
    """A utilisation bar is diagnostic. The person answering "why are we getting 429s" is
    often not the person allowed to raise the limit."""
    world = directory.world
    response = await directory.as_user(
        world.acme_viewer, "GET", f"/api/v1/gateways/{world.acme_gateway.id}/limits"
    )

    assert response.status_code == 200


async def test_the_bars_report_what_has_actually_been_spent(
    directory: DirectoryHarness,
) -> None:
    """Read live from the buckets the limiter consumes from, so a bar at 100% and a 429
    in the client's log are the same fact rather than two systems that usually agree."""
    world = directory.world
    world.acme_gateway.limits = {"requests_per_minute": 10}
    spend(world, "requests_per_minute", 4)

    response = await directory.as_user(
        world.acme_admin, "GET", f"/api/v1/gateways/{world.acme_gateway.id}/limits"
    )

    usage = {row["limit"]: row for row in response.json()["usage"]}
    assert usage["requests_per_minute"]["used"] == 4
    assert usage["requests_per_minute"]["remaining"] == 6


async def test_an_unlimited_gateway_reports_no_bars(directory: DirectoryHarness) -> None:
    """Nothing to draw, rather than four bars at zero — which would read as a gateway
    that is limited and idle."""
    world = directory.world
    response = await directory.as_user(
        world.acme_admin, "GET", f"/api/v1/gateways/{world.acme_gateway.id}/limits"
    )

    assert response.json()["usage"] == []


async def test_a_gateway_that_does_not_exist_is_a_404(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.acme_admin, "GET", f"/api/v1/gateways/{uuid.uuid4()}/limits"
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# the dashboard's warning card
# ---------------------------------------------------------------------------


async def test_nothing_under_pressure_is_an_empty_list(directory: DirectoryHarness) -> None:
    response = await directory.as_user(directory.world.acme_admin, "GET", "/api/v1/limits/pressure")

    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_a_gateway_running_hot_appears_on_the_card(directory: DirectoryHarness) -> None:
    world = directory.world
    world.acme_gateway.limits = {"requests_per_minute": 10}
    spend(world, "requests_per_minute", 9)

    response = await directory.as_user(world.acme_admin, "GET", "/api/v1/limits/pressure")

    items = response.json()["items"]
    assert [item["slug"] for item in items] == [world.acme_gateway.slug]
    assert items[0]["worst"]["limit"] == "requests_per_minute"


async def test_a_gateway_well_inside_its_budget_does_not(directory: DirectoryHarness) -> None:
    """80% is a warning. A card that lit up at 40% would be a card people turn off."""
    world = directory.world
    world.acme_gateway.limits = {"requests_per_minute": 10}
    spend(world, "requests_per_minute", 4)

    response = await directory.as_user(world.acme_admin, "GET", "/api/v1/limits/pressure")

    assert response.json()["items"] == []


async def test_another_organizations_pressure_is_not_on_this_card(
    directory: DirectoryHarness,
) -> None:
    world = directory.world
    world.acme_gateway.limits = {"requests_per_minute": 10}
    spend(world, "requests_per_minute", 9)

    response = await directory.as_user(world.globex_admin, "GET", "/api/v1/limits/pressure")

    assert response.json()["items"] == []


# ---------------------------------------------------------------------------
# the platform ceiling, on the way in
# ---------------------------------------------------------------------------


@pytest.fixture
def capped() -> World:
    return build_world(settings=with_ceilings(requests_per_minute=60, tokens_per_minute=10_000))


async def test_a_limit_above_the_ceiling_is_refused_on_a_global_model(capped: World) -> None:
    """Task 14's acceptance criterion. The credential being spent is the operator's, so
    this is not the organization's number to choose."""
    with pytest.raises(Validation) as failure:
        await capped.gateways.create_gateway(
            capped.actor(capped.acme_admin),
            GatewayDraft(
                name="Hot",
                slug="acme-hot",
                targets=(TargetSpec(model_id=capped.global_model.id),),
                limits={"requests_per_minute": 5000},
            ),
        )

    assert failure.value.param == "limits.requests_per_minute"
    assert "60" in failure.value.message


async def test_the_same_limit_is_allowed_on_the_organizations_own_model(capped: World) -> None:
    """The ceiling protects the platform's key. An org paying its own provider bill may
    set whatever it likes."""
    view = await capped.gateways.create_gateway(
        capped.actor(capped.acme_admin),
        GatewayDraft(
            name="Ours",
            slug="acme-ours",
            targets=(TargetSpec(model_id=capped.acme_model.id),),
            limits={"requests_per_minute": 5000},
        ),
    )

    assert view.gateway.limits["requests_per_minute"] == 5000


async def test_a_limit_at_the_ceiling_is_accepted(capped: World) -> None:
    view = await capped.gateways.create_gateway(
        capped.actor(capped.acme_admin),
        GatewayDraft(
            name="Fine",
            slug="acme-fine",
            targets=(TargetSpec(model_id=capped.global_model.id),),
            limits={"requests_per_minute": 60},
        ),
    )

    assert view.gateway.limits["requests_per_minute"] == 60


async def test_raising_a_limit_by_patch_is_refused_too(capped: World) -> None:
    """The obvious way around a create-time check."""
    actor = capped.actor(capped.acme_admin)
    view = await capped.gateways.create_gateway(
        actor,
        GatewayDraft(
            name="Fine", slug="acme-fine", targets=(TargetSpec(model_id=capped.global_model.id),)
        ),
    )

    with pytest.raises(Validation):
        await capped.gateways.update_gateway(
            actor, view.gateway.id, GatewayPatch(limits={"requests_per_minute": 5000})
        )


async def test_swapping_in_a_global_model_and_raising_the_limit_at_once_is_refused(
    capped: World,
) -> None:
    """The two halves have to be checked together, or the save that does both slips
    through: neither one is over the ceiling on its own."""
    actor = capped.actor(capped.acme_admin)
    view = await capped.gateways.create_gateway(
        actor,
        GatewayDraft(
            name="Ours", slug="acme-ours", targets=(TargetSpec(model_id=capped.acme_model.id),)
        ),
    )

    with pytest.raises(Validation):
        await capped.gateways.update_gateway(
            actor,
            view.gateway.id,
            GatewayPatch(
                targets=(TargetSpec(model_id=capped.global_model.id),),
                limits={"requests_per_minute": 5000},
            ),
        )


async def test_an_unset_limit_is_not_refused_even_though_the_ceiling_binds_it(
    capped: World,
) -> None:
    """Silently *enforced* at the ceiling rather than refused — see
    ``app.services.limits.effective``. Refusing here would make it impossible to create a
    gateway on a global model without first inventing a number."""
    view = await capped.gateways.create_gateway(
        capped.actor(capped.acme_admin),
        GatewayDraft(
            name="Fine", slug="acme-fine", targets=(TargetSpec(model_id=capped.global_model.id),)
        ),
    )

    assert view.gateway.limits["requests_per_minute"] is None


async def test_the_screen_says_the_ceiling_lowered_the_number(capped: World) -> None:
    """Without this the editor looks like it is ignoring the value that was saved."""
    actor = capped.actor(capped.acme_admin)
    await capped.gateways.update_gateway(
        actor,
        capped.acme_gateway.id,
        GatewayPatch(targets=(TargetSpec(model_id=capped.global_model.id),)),
    )

    view = await capped.auth.limits.gateway(actor, capped.acme_gateway.id)

    assert view.configured.requests_per_minute is None
    assert view.enforced.gateway.requests_per_minute == 60
    assert "requests_per_minute" in view.enforced.capped
    assert view.global_models is True


async def test_a_gateway_on_its_own_models_is_untouched_by_the_ceiling(capped: World) -> None:
    view = await capped.auth.limits.gateway(capped.actor(capped.acme_admin), capped.acme_gateway.id)

    assert view.global_models is False
    assert view.enforced.capped == ()
    # The numbers are still reported, so the editor can explain what *would* happen.
    assert view.ceilings.requests_per_minute == 60


# ---------------------------------------------------------------------------
# who is being throttled
# ---------------------------------------------------------------------------


def throttle(world: World, external_id: str, times: int) -> uuid.UUID:
    who = make_end_user(world.acme, external_id)
    world.database.end_users[who.id] = who
    for _ in range(times):
        row = make_log_row(
            world.acme,
            gateway_id=world.acme_gateway.id,
            created_at=THROTTLED_AT,
            status_code=429,
            error_code="rate_limited",
            end_user_id=who.id,
        )
        world.database.request_logs[row.id] = row
    return who.id


async def test_the_throttled_list_names_the_worst_caller_first(
    directory: DirectoryHarness,
) -> None:
    world = directory.world
    throttle(world, "quiet-app", 1)
    noisy = throttle(world, "noisy-bot", 3)
    # `+00:00` would be read as a space in a query string, so the offset is written the
    # other legal way round.
    window = (
        f"from={(THROTTLED_AT - timedelta(minutes=1)).isoformat().replace('+00:00', 'Z')}"
        f"&to={(THROTTLED_AT + timedelta(minutes=1)).isoformat().replace('+00:00', 'Z')}"
    )

    response = await directory.as_user(
        world.acme_admin, "GET", f"/api/v1/metrics/throttled?{window}"
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert items[0]["end_user_id"] == str(noisy)
    assert items[0]["external_id"] == "noisy-bot"
    assert items[0]["rejections"] == 3


async def test_the_throttled_list_is_empty_when_nothing_was_refused(
    directory: DirectoryHarness,
) -> None:
    response = await directory.as_user(
        directory.world.acme_admin, "GET", "/api/v1/metrics/throttled"
    )

    assert response.json()["items"] == []


async def test_a_viewer_may_read_the_throttled_list(directory: DirectoryHarness) -> None:
    response = await directory.as_user(
        directory.world.acme_viewer, "GET", "/api/v1/metrics/throttled"
    )

    assert response.status_code == 200
