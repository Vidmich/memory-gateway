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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.patterns import UnsafePattern, check_pattern
from app.schemas.config import CONFIG_VERSION, ConfigBlob, merge_config

#: SPEC §11's four caps, in the order they are checked and shown. Requests before
#: tokens before concurrency: counting is free, estimating tokens is not, and a slot is
#: the only one of the four that has to be given back afterwards.
LIMIT_NAMES: tuple[str, ...] = (
    "requests_per_minute",
    "requests_per_day",
    "tokens_per_minute",
    "concurrent_requests",
)

MAX_REDACTION_PATTERNS = 20
MAX_REDACTION_PATTERN_LENGTH = 200

#: Where an organization's logging defaults live inside ``organizations.settings``
#: (SPEC §10.2). A key inside the existing blob rather than a column, for the same reason
#: the blob exists: tasks 13 and 17 put their own defaults beside it.
ORG_LOGGING_DEFAULTS = "logging_defaults"


class MemoryConfig(ConfigBlob):
    """SPEC §6.3 — what this gateway retrieves, and what happens when it cannot.

    ``connector_ids`` is empty by default, which means no document memory — a gateway
    that silently started reading every connector in the organization would be a
    disclosure bug, so the safe default is "nothing". Empty is also a *fast* path rather
    than a filter that matches nothing: :mod:`app.services.retrieval` skips the embedding
    and the vector call entirely, so an unconfigured gateway costs no latency at all.

    ``on_retrieval_error`` is the only field here that is about the product rather than
    about quality, and it is a real choice with no safe default. ``fail_open`` serves an
    ungrounded answer when the index is unreachable, which is right for a support bot
    that is better than nothing; ``fail_closed`` returns 503, which is right for an
    assistant whose whole value is that it only answers from the handbook. The platform
    default is ``fail_open`` because availability is the more common preference, and the
    editor says in words what the other one does.
    """

    connector_ids: list[uuid.UUID] = Field(default_factory=list)
    doc_top_k: int = Field(default=6, ge=1, le=100)
    doc_min_score: float = Field(default=0.35, ge=0.0, le=1.0)
    doc_max_tokens: int = Field(default=2000, ge=0, le=100_000)
    memory_enabled: bool = True
    memory_top_k: int = Field(default=8, ge=1, le=100)
    memory_max_tokens: int = Field(default=600, ge=0, le=100_000)
    #: The floor for the similarity half of recall. Lower than ``doc_min_score`` on
    #: purpose: a fact is one short sentence, so it shares far less vocabulary with a
    #: question than a thousand-token chunk does, and a chunk's floor applied to a fact
    #: would reject "works in the EU" for every question that is not about the EU.
    memory_min_score: float = Field(default=0.3, ge=0.0, le=1.0)
    #: SPEC §6.2's third identity source, off by default. An IP-derived id merges everyone
    #: behind one office NAT into a single person and splits one person across two
    #: networks — a coarse, surprising basis for something that stores durable personal
    #: facts, and not a thing to switch on for somebody without their asking.
    allow_anonymous_memory: bool = False
    query_strategy: Literal["last_user_message", "last_n_turns"] = "last_user_message"
    #: Read only by ``last_n_turns``. Stored whatever the strategy, so switching to
    #: ``last_user_message`` to compare and back does not lose the number somebody tuned.
    query_n_turns: int = Field(default=3, ge=1, le=20)
    #: SPEC §6.3's 800 ms. The ceiling is 5 s rather than unbounded: a retrieval timeout
    #: is added to every request that uses this gateway, and a value large enough to be
    #: invisible in testing is large enough to make a provider outage look like a gateway
    #: outage.
    retrieval_timeout_ms: int = Field(default=800, ge=50, le=5000)
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
    #: Whether *this endpoint's* traffic feeds conversation memory. Not the same switch
    #: as :attr:`app.schemas.distillation.DistillationConfig.enabled`, which is the
    #: organization's: a gateway serving an internal batch job should teach the assistant
    #: nothing while the rest of the organization goes on learning.
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


class Quota(BaseModel):
    """SPEC §11's four caps. ``None`` means unlimited, everywhere.

    A separate model from :class:`LimitsConfig` because the same four fields are asked
    twice — once of the endpoint and once of each person using it — and writing them
    twice is how the two drift apart.
    """

    model_config = ConfigDict(extra="ignore")

    requests_per_minute: int | None = Field(default=None, ge=1)
    #: Prompt **plus** completion, per SPEC §11 — so a gateway answering with essays is
    #: throttled by the same number as one being asked them.
    tokens_per_minute: int | None = Field(default=None, ge=1)
    concurrent_requests: int | None = Field(default=None, ge=1)
    requests_per_day: int | None = Field(default=None, ge=1)

    @property
    def unlimited(self) -> bool:
        return all(getattr(self, name) is None for name in LIMIT_NAMES)


class LimitsConfig(ConfigBlob):
    """SPEC §11. Unlimited by default, which is the v1 default and not an oversight — a
    limit that quietly existed before anyone set one would be a surprise outage rather
    than a policy.

    The four gateway-scope fields are at the top level rather than under a ``gateway``
    key, because they were written that way before ``per_end_user`` existed and moving
    them would silently unset the limits of every gateway already configured. The shape
    is a little lopsided; a migration of live traffic limits is worse.

    ``per_end_user`` applies the same four caps to one ``X-Gateway-User`` at a time. It
    is not a subdivision of the gateway's budget — both are checked, and either refuses —
    so a per-person cap on an otherwise unlimited gateway is a sensible configuration and
    is not silently raised to meet it.
    """

    requests_per_minute: int | None = Field(default=None, ge=1)
    tokens_per_minute: int | None = Field(default=None, ge=1)
    concurrent_requests: int | None = Field(default=None, ge=1)
    requests_per_day: int | None = Field(default=None, ge=1)
    per_end_user: Quota = Field(default_factory=Quota)

    @property
    def gateway(self) -> Quota:
        """The top-level four, as the same shape as :attr:`per_end_user`.

        Everything downstream — planning, the ceiling, the utilisation screen — works in
        terms of two quotas, so the lopsidedness described above stops here rather than
        being threaded through six more modules.
        """
        return Quota(
            requests_per_minute=self.requests_per_minute,
            tokens_per_minute=self.tokens_per_minute,
            concurrent_requests=self.concurrent_requests,
            requests_per_day=self.requests_per_day,
        )


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
    "LIMIT_NAMES",
    "ORG_LOGGING_DEFAULTS",
    "ConfigBlob",
    "LimitsConfig",
    "LoggingConfig",
    "MemoryConfig",
    "Quota",
    "merge_config",
    "organization_logging_defaults",
]
