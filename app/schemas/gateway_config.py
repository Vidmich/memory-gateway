"""The three configuration blobs a gateway carries, as versioned schemas.

``memory_config``, ``logging_config`` and ``limits`` are JSONB columns whose contents are
owned by tasks 10, 07 and 14. They are created here, in full, with the defaults from the
SPEC — so those tasks add behaviour and a field, not a migration on a live table.

The mechanics that make that safe — permissive on load, strict on write, defaults as
documentation — live in :mod:`app.schemas.config` and are shared with every other
settings blob in the product. This module is only the gateway's three shapes.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from app.core.patterns import UnsafePattern, check_pattern
from app.schemas.config import CONFIG_VERSION, ConfigBlob, merge_config

MAX_REDACTION_PATTERNS = 20
MAX_REDACTION_PATTERN_LENGTH = 200

#: Where an organization's logging defaults live inside ``organizations.settings``
#: (SPEC §10.2). A key inside the existing blob rather than a column, for the same reason
#: the blob exists: tasks 13 and 17 put their own defaults beside it.
ORG_LOGGING_DEFAULTS = "logging_defaults"


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
            # Compiles *and* refuses the shapes that backtrack catastrophically. The
            # patterns run in the log flusher against whatever a customer's end users
            # typed, and Python's `re` cannot be interrupted once it is matching — so
            # the only place to stop `(a+)+$` is the form it is being typed into.
            try:
                check_pattern(pattern)
            except UnsafePattern as exc:
                raise ValueError(str(exc)) from exc

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


def organization_logging_defaults(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    """The logging defaults a new gateway in this organization starts from.

    Partial, and merged over the platform defaults rather than replacing them: an
    organization that only wants ``log_response_body`` off should not also have to restate
    the retention it never had an opinion about.

    Anything that is not an object is ignored rather than refused. This value is read
    while creating a gateway, and a settings blob that somebody hand-edited badly should
    not make the Gateways screen stop working — it should make the defaults not apply,
    which is the state the organization was in before they edited it.
    """
    if not isinstance(settings, Mapping):
        return {}
    defaults = settings.get(ORG_LOGGING_DEFAULTS)
    return dict(defaults) if isinstance(defaults, Mapping) else {}


__all__ = [
    "CONFIG_VERSION",
    "ORG_LOGGING_DEFAULTS",
    "ConfigBlob",
    "LimitsConfig",
    "LoggingConfig",
    "MemoryConfig",
    "merge_config",
    "organization_logging_defaults",
]
