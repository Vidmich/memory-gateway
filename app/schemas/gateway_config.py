"""The three configuration blobs a gateway carries, as versioned schemas.

``memory_config``, ``logging_config`` and ``limits`` are JSONB columns whose contents are
owned by tasks 10, 07 and 14. They are created here, in full, with the defaults from the
SPEC — so those tasks add behaviour and a field, not a migration on a live table.

Two properties make that safe:

**Every field has a default, and unknown keys are ignored on load.** A row written before
a field existed loads with the default; a row written by a *newer* build that has since
been rolled back loads without its extra keys instead of crashing the read path. That is
the whole reason for :meth:`ConfigBlob.load` being separate from ordinary validation.

**A write is strict.** :func:`merge_config` refuses a key the schema does not define, so
``{"doc_top_kk": 8}`` is a 422 on the form rather than a setting that silently never
applies — the same rule, and the same reasoning, as the parameter allowlist in
:mod:`app.services.params`.

The defaults are also the *documentation*: a gateway created today stores ``{}`` and
answers with the full object, so the API always shows what will actually happen.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.errors import Validation

#: Bumped when a field changes meaning, not when one is added. Stored on the row so a
#: future migration can tell "never written" from "written by version 1".
CONFIG_VERSION = 1

MAX_REDACTION_PATTERNS = 20
MAX_REDACTION_PATTERN_LENGTH = 200


class ConfigBlob(BaseModel):
    """Base for the three blobs. Permissive on load, strict on write."""

    # Ignore rather than forbid: this class is what a *stored* row is parsed with, and a
    # row from a newer build must still load. `merge_config` does the strict check.
    model_config = ConfigDict(extra="ignore")

    version: int = CONFIG_VERSION

    @classmethod
    def load(cls, stored: Mapping[str, Any] | None) -> Self:
        """Parse a stored blob, filling in every default. ``{}`` is a valid input."""
        return cls.model_validate(dict(stored or {}))


class MemoryConfig(ConfigBlob):
    """SPEC §6.3. Task 10 makes these do something; the shape is settled now.

    ``connector_ids`` is empty by default, which means no document memory — a gateway
    that silently started reading every connector in the organization would be a
    disclosure bug, so the safe default is "nothing".
    """

    connector_ids: list[uuid.UUID] = Field(default_factory=list)
    doc_top_k: int = Field(default=6, ge=1, le=100)
    doc_min_score: float = Field(default=0.35, ge=0.0, le=1.0)
    doc_max_tokens: int = Field(default=2000, ge=0, le=100_000)
    memory_enabled: bool = True
    memory_top_k: int = Field(default=8, ge=1, le=100)
    memory_max_tokens: int = Field(default=600, ge=0, le=100_000)
    query_strategy: Literal["last_user_message", "last_n_turns"] = "last_user_message"
    on_retrieval_error: Literal["fail_open", "fail_closed"] = "fail_open"


class LoggingConfig(ConfigBlob):
    """SPEC §10.2. Full capture by default — bodies are what make distillation possible
    — and every part switchable."""

    log_metadata: bool = True
    log_request_body: bool = True
    log_assembled_prompt: bool = True
    log_response_body: bool = True
    retention_days: int = Field(default=30, ge=1, le=3650)
    metadata_retention_days: int = Field(default=365, ge=1, le=3650)
    #: Applied to bodies before persistence. Compiled here so a broken pattern is a 422
    #: on the form, not an exception on the logging path of somebody's live traffic.
    redaction_patterns: list[str] = Field(default_factory=list)
    enable_distillation: bool = True

    @model_validator(mode="after")
    def _check(self) -> Self:
        if len(self.redaction_patterns) > MAX_REDACTION_PATTERNS:
            raise ValueError(f"at most {MAX_REDACTION_PATTERNS} redaction patterns")
        for pattern in self.redaction_patterns:
            if len(pattern) > MAX_REDACTION_PATTERN_LENGTH:
                raise ValueError(
                    f"a redaction pattern is at most {MAX_REDACTION_PATTERN_LENGTH} characters long"
                )
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"'{pattern}' is not a valid regular expression: {exc}") from exc

        if self.enable_distillation and not self.log_request_body:
            # SPEC §10.2: distillation reads transcripts. Accepting the combination would
            # produce a gateway whose screen promises memory it can never build.
            raise ValueError(
                "distillation reads logged request bodies; enable body logging or turn "
                "distillation off"
            )
        if self.metadata_retention_days < self.retention_days:
            # Metadata is the cheap half and the half the monitoring screens read.
            # Keeping it for less time than the bodies it describes leaves orphans.
            raise ValueError("metadata retention must be at least as long as body retention")
        return self


class LimitsConfig(ConfigBlob):
    """SPEC §11. ``None`` means unlimited, which is the v1 default — task 14 enforces
    these, and a limit that quietly existed before anyone set one would be a surprise
    outage rather than a policy."""

    requests_per_minute: int | None = Field(default=None, ge=1)
    tokens_per_minute: int | None = Field(default=None, ge=1)
    concurrent_requests: int | None = Field(default=None, ge=1)
    requests_per_day: int | None = Field(default=None, ge=1)


def merge_config[BlobT: ConfigBlob](
    schema: type[BlobT],
    stored: Mapping[str, Any] | None,
    patch: Mapping[str, Any] | None,
    *,
    field: str,
) -> dict[str, Any]:
    """Deep-merge ``patch`` into ``stored`` and validate the result against ``schema``.

    A partial update, so ``{"doc_top_k": 8}`` changes one knob and leaves the rest alone.
    Unknown keys are refused rather than merged: a stored setting nothing reads is
    indistinguishable from a setting that does not work, and the second is what the user
    will conclude.

    ``field`` names the form field, so the 422 lands on the section the user is editing
    instead of in a banner.
    """
    merged = _deep_merge(dict(stored or {}), dict(patch or {}))
    _reject_unknown(schema, merged, field=field)
    try:
        blob = schema.model_validate(merged)
    except Exception as exc:
        raise Validation(_first_problem(exc), param=field) from exc
    return blob.model_dump(mode="json")


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursive for nested objects; a list is replaced wholesale.

    Today's blobs are flat, so this is one level in practice. It is written recursively
    because task 10's retrieval settings are the obvious place for a nested object, and a
    shallow merge that silently discarded its siblings would be found the hard way.
    """
    result = dict(base)
    for key, value in patch.items():
        current = result.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            result[key] = _deep_merge(current, value)
        else:
            # Lists are replaced: there is no sensible "merge" of two redaction pattern
            # lists, and appending would make removing one impossible.
            result[key] = value
    return result


def _reject_unknown(schema: type[ConfigBlob], merged: Mapping[str, Any], *, field: str) -> None:
    known = set(schema.model_fields)
    for key in merged:
        if key not in known:
            raise Validation(
                f"'{key}' is not a setting on this section. "
                f"Allowed: {', '.join(sorted(known - {'version'}))}.",
                param=f"{field}.{key}",
            )


def _first_problem(exc: Exception) -> str:
    """The first validation message, without Pydantic's envelope around it."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        for error in errors():
            location = ".".join(str(part) for part in error.get("loc", ()))
            message = str(error.get("msg", "")).removeprefix("Value error, ")
            return f"{location}: {message}" if location else message
    return str(exc)


__all__ = [
    "CONFIG_VERSION",
    "ConfigBlob",
    "LimitsConfig",
    "LoggingConfig",
    "MemoryConfig",
    "merge_config",
]
