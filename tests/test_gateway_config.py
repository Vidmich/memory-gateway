"""The three versioned config blobs.

The property under test is forward and backward tolerance. These columns are written by
one build and read by another — a rollback, a rolling deploy, a row from six months ago —
and the read path must never be the thing that breaks. So: every field has a default,
unknown keys are ignored on *load*, and refused on *write*.

The asymmetry is the whole design, and it is easy to get backwards.
"""

from __future__ import annotations

import pytest

from app.core.errors import Validation
from app.schemas.gateway_config import (
    CONFIG_VERSION,
    ConfigBlob,
    LimitsConfig,
    LoggingConfig,
    MemoryConfig,
    merge_config,
)

BLOBS: tuple[type[ConfigBlob], ...] = (MemoryConfig, LoggingConfig, LimitsConfig)


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("schema", BLOBS, ids=lambda schema: schema.__name__)
async def test_an_empty_blob_loads_with_defaults(schema: type[ConfigBlob]) -> None:
    """A gateway created before this task stores ``{}``, and must still serve."""
    loaded = schema.load({})

    assert loaded.version == CONFIG_VERSION


@pytest.mark.parametrize("schema", BLOBS, ids=lambda schema: schema.__name__)
async def test_a_null_blob_loads(schema: type[ConfigBlob]) -> None:
    assert schema.load(None).version == CONFIG_VERSION


@pytest.mark.parametrize("schema", BLOBS, ids=lambda schema: schema.__name__)
async def test_a_field_from_a_newer_build_is_ignored_on_load(
    schema: type[ConfigBlob],
) -> None:
    """A rolled-back deploy reads rows the newer one wrote. Ignoring what it does not
    understand is the difference between a degraded feature and a broken read path."""
    loaded = schema.load({"a_field_from_the_future": 42})

    assert not hasattr(loaded, "a_field_from_the_future")


async def test_a_missing_field_takes_its_default() -> None:
    loaded = MemoryConfig.load({"doc_top_k": 12})

    assert loaded.doc_top_k == 12
    assert loaded.doc_min_score == MemoryConfig().doc_min_score


# ---------------------------------------------------------------------------
# merging
# ---------------------------------------------------------------------------


async def test_a_patch_changes_one_key_and_keeps_the_rest() -> None:
    stored = merge_config(MemoryConfig, {}, {"doc_top_k": 12}, field="memory_config")

    merged = merge_config(MemoryConfig, stored, {"memory_top_k": 3}, field="memory_config")

    assert merged["doc_top_k"] == 12
    assert merged["memory_top_k"] == 3


async def test_a_list_is_replaced_not_appended() -> None:
    """There is no sensible merge of two redaction-pattern lists, and appending would
    make removing one impossible."""
    stored = merge_config(
        LoggingConfig, {}, {"redaction_patterns": [r"\\d{16}"]}, field="logging_config"
    )

    merged = merge_config(LoggingConfig, stored, {"redaction_patterns": []}, field="logging_config")

    assert merged["redaction_patterns"] == []


async def test_an_empty_patch_leaves_everything_alone() -> None:
    stored = merge_config(LimitsConfig, {}, {"requests_per_minute": 60}, field="limits")

    merged = merge_config(LimitsConfig, stored, {}, field="limits")

    assert merged["requests_per_minute"] == 60


async def test_a_nested_object_is_merged_rather_than_replaced() -> None:
    """Today's blobs are flat, so this exercises the mechanism rather than a field. Task
    10's retrieval settings are the obvious place for a nested object, and a shallow merge
    that discarded its siblings would be found the hard way."""
    from app.schemas.config import _deep_merge

    merged = _deep_merge({"outer": {"kept": 1, "changed": 2}}, {"outer": {"changed": 3}})

    assert merged == {"outer": {"kept": 1, "changed": 3}}


# ---------------------------------------------------------------------------
# writing is strict
# ---------------------------------------------------------------------------


async def test_an_unknown_key_is_refused_on_write() -> None:
    """The asymmetry: ignored on load, refused on write. A stored setting nothing reads is
    indistinguishable from a setting that does not work, and the second is what the user
    will conclude."""
    with pytest.raises(Validation) as raised:
        merge_config(MemoryConfig, {}, {"doc_top_kk": 8}, field="memory_config")

    assert raised.value.param == "memory_config.doc_top_kk"


async def test_a_value_out_of_range_is_refused() -> None:
    """The param names the section *and* the field inside it.

    The section keeps the message unambiguous when two blobs share a field name; the leaf
    is what a form can put the message next to, because the section is not an input.
    """
    with pytest.raises(Validation) as raised:
        merge_config(MemoryConfig, {}, {"doc_min_score": 2.0}, field="memory_config")

    assert raised.value.param == "memory_config.doc_min_score"


async def test_an_unknown_enum_value_is_refused() -> None:
    with pytest.raises(Validation):
        merge_config(
            MemoryConfig, {}, {"query_strategy": "whole_conversation"}, field="memory_config"
        )


async def test_a_stored_key_from_a_newer_build_blocks_a_write() -> None:
    """Deliberate. Merging into a row this build does not understand would drop the
    field it cannot see, so refusing is the honest answer — and it only happens during a
    rollback, where a failed save is better than silent data loss."""
    with pytest.raises(Validation):
        merge_config(MemoryConfig, {"from_the_future": 1}, {"doc_top_k": 8}, field="memory_config")


# ---------------------------------------------------------------------------
# the rules with a reason behind them
# ---------------------------------------------------------------------------


async def test_distillation_requires_body_logging() -> None:
    """SPEC §10.2. Accepting the pair would produce a gateway whose screen promises
    memory it can never build."""
    with pytest.raises(Validation) as raised:
        merge_config(LoggingConfig, {}, {"log_request_body": False}, field="logging_config")

    assert "distillation" in raised.value.message


async def test_turning_distillation_off_makes_that_combination_legal() -> None:
    merged = merge_config(
        LoggingConfig,
        {},
        {"log_request_body": False, "enable_distillation": False},
        field="logging_config",
    )

    assert merged["log_request_body"] is False


async def test_metadata_retention_cannot_be_shorter_than_body_retention() -> None:
    """Metadata is the cheap half and the half the monitoring screens read; keeping it
    for less time than the bodies it describes leaves orphan transcripts."""
    with pytest.raises(Validation):
        merge_config(
            LoggingConfig,
            {},
            {"retention_days": 90, "metadata_retention_days": 30},
            field="logging_config",
        )


async def test_a_broken_redaction_pattern_is_refused() -> None:
    """Compiled at write time, so a bad regex is a 422 on the form rather than an
    exception on the logging path of somebody's live traffic."""
    with pytest.raises(Validation) as raised:
        merge_config(
            LoggingConfig, {}, {"redaction_patterns": ["(unclosed"]}, field="logging_config"
        )

    assert "regular expression" in raised.value.message


async def test_a_valid_redaction_pattern_is_accepted() -> None:
    merged = merge_config(
        LoggingConfig,
        {},
        {"redaction_patterns": [r"\b\d{16}\b", r"[\w.]+@[\w.]+"]},
        field="logging_config",
    )

    assert len(merged["redaction_patterns"]) == 2


async def test_too_many_redaction_patterns_are_refused() -> None:
    with pytest.raises(Validation):
        merge_config(
            LoggingConfig,
            {},
            {"redaction_patterns": [f"pattern{index}" for index in range(50)]},
            field="logging_config",
        )


# ---------------------------------------------------------------------------
# defaults are the documentation
# ---------------------------------------------------------------------------


async def test_memory_defaults_match_the_spec() -> None:
    defaults = MemoryConfig()

    assert (defaults.doc_top_k, defaults.doc_min_score, defaults.doc_max_tokens) == (6, 0.35, 2000)
    assert (defaults.memory_enabled, defaults.memory_top_k, defaults.memory_max_tokens) == (
        True,
        8,
        600,
    )
    assert defaults.on_retrieval_error == "fail_open"


async def test_no_connectors_are_readable_by_default() -> None:
    """A gateway that silently started reading every connector in the organization would
    be a disclosure bug, so the safe default is nothing."""
    assert MemoryConfig().connector_ids == []


async def test_logging_defaults_to_full_capture() -> None:
    defaults = LoggingConfig()

    assert defaults.log_request_body and defaults.log_assembled_prompt
    assert (defaults.retention_days, defaults.metadata_retention_days) == (30, 365)


async def test_limits_default_to_unlimited() -> None:
    """Task 14 enforces these. A limit that quietly existed before anyone set one would be
    a surprise outage rather than a policy — which is why the per-end-user block defaults
    the same way rather than inheriting the gateway's numbers."""
    assert LimitsConfig().model_dump(exclude={"version"}) == {
        "requests_per_minute": None,
        "tokens_per_minute": None,
        "concurrent_requests": None,
        "requests_per_day": None,
        "per_end_user": {
            "requests_per_minute": None,
            "tokens_per_minute": None,
            "concurrent_requests": None,
            "requests_per_day": None,
        },
    }
