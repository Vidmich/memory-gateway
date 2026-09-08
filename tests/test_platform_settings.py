"""Precedence, validation, and the cache in front of both.

The precedence rule is the whole design and it has exactly three states worth asserting:
nothing configured (the environment), something configured (the database), and something
configured *badly* (the environment again, loudly). A settings layer that got the third
one wrong would take the platform down on a rollback.

The ceiling tests are the other half. ``retention`` is a maximum an organization may be
stricter than and never longer, so the assertions are about direction rather than about
values — a ceiling that silently *raised* somebody's retention would pass a test that only
checked the number changed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.config import Settings, get_settings
from app.core.errors import Validation
from app.core.ids import uuid7
from app.core.tenancy import Actor, TenantScope
from app.schemas.platform import PlatformSettings, PlatformSettingsPatch
from app.services.memory_db import MemoryDatabase
from app.services.platform_settings import (
    SETTINGS_UPDATED,
    PlatformSettingsService,
    bootstrap,
    ceilings_of,
    effective_retention,
    resolve,
)
from app.services.platform_store import MemoryPlatformSettingsStore, StoredSetting
from tests.platform_support import PlatformFixture, build_platform


@pytest.fixture
def settings() -> Settings:
    return get_settings()


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


def stored(key: str, value: dict[str, object]) -> StoredSetting:
    return StoredSetting(key=key, value=value, updated_at=datetime.now(UTC))


# ---------------------------------------------------------------------------
# precedence
# ---------------------------------------------------------------------------


def test_an_empty_database_runs_on_the_environment(settings: Settings) -> None:
    """What a fresh deployment behaves like, and what every deployment behaved like before
    this table existed."""
    resolved = resolve(settings, ())

    assert resolved.embedding.provider == settings.embedding_provider
    assert resolved.embedding.name == settings.embedding_model
    assert resolved.embedding.dimension == settings.embedding_dimension
    assert resolved.storage.max_file_bytes == settings.upload_max_file_bytes


def test_a_row_overrides_the_variable_that_bootstrapped_it(settings: Settings) -> None:
    resolved = resolve(
        settings,
        [
            stored(
                "embedding",
                {"provider": "openai", "name": "text-embedding-3-large", "dimension": 3072},
            )
        ],
    )

    assert resolved.embedding.name == "text-embedding-3-large"
    assert resolved.embedding.dimension == 3072


def test_a_section_is_replaced_whole_rather_than_field_by_field(settings: Settings) -> None:
    """The atomicity the section grouping exists for.

    A model from the database and a dimension from the environment is precisely the
    mismatch that produces vectors Qdrant refuses — so a stored section replaces its
    environment counterpart entirely, and a field it omits falls back to the *schema's*
    default rather than to the variable.
    """
    resolved = resolve(settings, [stored("embedding", {"name": "text-embedding-3-small"})])

    assert resolved.embedding.name == "text-embedding-3-small"
    assert resolved.embedding.dimension == 256  # the schema default, not EMBEDDING_DIMENSION


def test_one_section_being_stored_leaves_the_others_on_the_environment(
    settings: Settings,
) -> None:
    resolved = resolve(settings, [stored("retention", {"max_body_days": 30})])

    assert resolved.retention.max_body_days == 30
    assert resolved.embedding.name == settings.embedding_model


def test_a_row_that_no_longer_validates_falls_back_rather_than_raising(
    settings: Settings,
) -> None:
    """This function runs at startup and on the request path. A settings row written by a
    newer build and then rolled back must not stop the platform serving traffic."""
    resolved = resolve(settings, [stored("embedding", {"dimension": -5})])

    assert resolved.embedding.name == settings.embedding_model


def test_a_section_nobody_recognises_is_ignored(settings: Settings) -> None:
    resolved = resolve(settings, [stored("telemetry", {"enabled": True})])

    assert resolved == resolve(settings, ())


def test_the_bootstrap_names_only_sections_the_environment_actually_configures(
    settings: Settings,
) -> None:
    """``logging`` is absent on purpose: there is no environment variable for a gateway's
    default logging policy, so its defaults are the schema's and stay that way until
    somebody sets them on the screen."""
    assert set(bootstrap(settings)) == {"embedding", "distillation", "limits", "storage"}


# ---------------------------------------------------------------------------
# the cache
# ---------------------------------------------------------------------------


async def test_the_snapshot_is_available_before_anything_has_been_read(
    settings: Settings,
) -> None:
    """Nothing on the request path has to handle "settings not loaded yet"; the worst case
    is one interval of pre-task-17 behaviour, which is a defined state."""
    service = PlatformSettingsService(
        MemoryPlatformSettingsStore(MemoryDatabase()), settings=settings
    )

    assert service.loaded is False
    assert service.snapshot.embedding.name == settings.embedding_model


async def test_a_write_updates_the_snapshot_immediately(
    platform: PlatformFixture, actor: Actor
) -> None:
    """The operator who just pressed Save must not see the old number."""
    await platform.platform_settings.update(
        actor, PlatformSettingsPatch(retention={"max_body_days": 14})
    )

    assert platform.platform_settings.snapshot.retention.max_body_days == 14


async def test_a_refresh_picks_up_a_write_made_elsewhere(
    platform: PlatformFixture, actor: Actor
) -> None:
    """What bounds staleness on the other replicas: they re-read on their own interval."""
    other = PlatformSettingsService(platform.settings_store, settings=platform.settings)
    await other.refresh()
    await platform.platform_settings.update(
        actor, PlatformSettingsPatch(storage={"quota_bytes": 4096})
    )

    assert other.snapshot.storage.quota_bytes is None
    await other.refresh()
    assert other.snapshot.storage.quota_bytes == 4096


async def test_warming_a_service_whose_database_is_down_does_not_raise(
    settings: Settings,
) -> None:
    """Refusing to start would turn a slow database into an outage of every replica."""

    class Broken:
        def begin(self) -> object:
            raise RuntimeError("no database")

    service = PlatformSettingsService(Broken(), settings=settings)  # type: ignore[arg-type]

    resolved = await service.warm()

    assert resolved.embedding.name == settings.embedding_model
    assert service.loaded is False


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


async def test_a_patch_touches_only_the_sections_it_names(
    platform: PlatformFixture, actor: Actor
) -> None:
    await platform.platform_settings.update(
        actor, PlatformSettingsPatch(storage={"quota_bytes": 8192})
    )

    view = await platform.platform_settings.view()

    assert view.settings.storage.quota_bytes == 8192
    assert "storage" not in view.from_environment
    assert "retention" in view.from_environment


async def test_a_key_the_section_does_not_define_is_refused(
    platform: PlatformFixture, actor: Actor
) -> None:
    """A stored setting nothing reads is indistinguishable from a setting that does not
    work, and the second is what the operator will conclude."""
    with pytest.raises(Validation) as refused:
        await platform.platform_settings.update(
            actor, PlatformSettingsPatch(retention={"max_body_dayz": 30})
        )

    assert refused.value.param == "retention.max_body_dayz"


async def test_an_empty_patch_is_refused_rather_than_recorded(
    platform: PlatformFixture, actor: Actor
) -> None:
    with pytest.raises(Validation):
        await platform.platform_settings.update(actor, PlatformSettingsPatch())


async def test_a_ceiling_pair_that_no_gateway_could_satisfy_is_refused(
    platform: PlatformFixture, actor: Actor
) -> None:
    """Bodies outliving the rows that describe them is the one combination nothing
    downstream can represent — and a form that refused every save with a message about a
    different field would be worse than the rule it was protecting."""
    with pytest.raises(Validation):
        await platform.platform_settings.update(
            actor,
            PlatformSettingsPatch(retention={"max_body_days": 90, "max_metadata_days": 30}),
        )


async def test_a_change_is_recorded_in_the_platforms_own_log(
    platform: PlatformFixture, actor: Actor
) -> None:
    """``organization_id`` is null: a platform-wide change in one customer's log would be
    both wrong and, for every other customer, invisible."""
    await platform.platform_settings.update(
        actor, PlatformSettingsPatch(retention={"max_body_days": 30})
    )

    events = [row for row in platform.db.audit_events.values() if row.action == SETTINGS_UPDATED]
    assert len(events) == 1
    assert events[0].organization_id is None
    assert events[0].target_label == "retention"
    assert events[0].actor_label == actor.label


async def test_the_diff_holds_only_the_sections_that_changed(
    platform: PlatformFixture, actor: Actor
) -> None:
    """A whole-blob before/after would render every section as unchanged noise around the
    one line somebody is looking for."""
    await platform.platform_settings.update(
        actor, PlatformSettingsPatch(storage={"quota_bytes": 2048})
    )

    event = next(row for row in platform.db.audit_events.values() if row.action == SETTINGS_UPDATED)
    fields = {change["path"] for change in event.diff["changes"]}
    assert all(field.startswith("storage") for field in fields), fields


async def test_a_distillation_model_that_is_not_a_global_one_is_refused(
    platform: PlatformFixture, actor: Actor
) -> None:
    """It is used by every organization, so a lookup that resolved to one tenant's model
    would be both a bill and a disclosure."""

    async def refuse(_: uuid.UUID) -> bool:
        return False

    service = PlatformSettingsService(
        platform.settings_store, settings=platform.settings, catalog_check=refuse
    )

    with pytest.raises(Validation) as refused:
        await service.update(actor, PlatformSettingsPatch(distillation={"model_id": str(uuid7())}))

    assert refused.value.param == "distillation.model_id"


# ---------------------------------------------------------------------------
# ceilings
# ---------------------------------------------------------------------------


def test_the_rate_limit_ceilings_translate_into_the_limiters_own_type() -> None:
    config = PlatformSettings.model_validate(
        {"limits": {"global_model_ceilings": {"requests_per_minute": 60}}}
    )

    assert ceilings_of(config).requests_per_minute == 60
    assert ceilings_of(config).tokens_per_minute is None


def test_a_gateway_below_the_retention_ceiling_is_left_alone() -> None:
    config = PlatformSettings.model_validate({"retention": {"max_body_days": 90}})

    bodies, metadata, capped = effective_retention(config, 30, 365)

    assert (bodies, metadata) == (30, 365)
    assert capped == ()


def test_a_gateway_above_the_ceiling_is_lowered_and_says_so() -> None:
    config = PlatformSettings.model_validate({"retention": {"max_body_days": 7}})

    bodies, _metadata, capped = effective_retention(config, 90, 365)

    assert bodies == 7
    assert capped == ("retention_days",)


def test_an_unset_ceiling_changes_nothing() -> None:
    """A limit nobody configured must not start deleting a customer's logs on upgrade."""
    bodies, metadata, capped = effective_retention(PlatformSettings(), 90, 365)

    assert (bodies, metadata, capped) == (90, 365, ())


def test_bodies_can_never_outlive_the_rows_that_describe_them() -> None:
    """Only the metadata ceiling is set, so the body window is capped by it as well —
    the one combination nothing downstream can represent."""
    config = PlatformSettings.model_validate({"retention": {"max_metadata_days": 10}})

    bodies, metadata, capped = effective_retention(config, 30, 365)

    assert (bodies, metadata) == (10, 10)
    assert "metadata_retention_days" in capped
