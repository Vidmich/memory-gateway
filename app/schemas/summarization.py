"""A connector's summarization settings (task 102, SPEC §9.3 as amended).

A chunk is embedded alone. Cut from the middle of a forty-page policy it says "the second
option requires approval above the threshold" and nothing about which policy or which
option, and the embedding model places the vector wherever twelve context-free words go.
Summarization gives the embedder that context back, in one of two shapes:

``summary_chunk``   one extra point per document, honestly labelled ``kind: summary``, so a
                    question *about the document* ("do we have a travel policy?") finds it.
``contextual``      the summary is prefixed to every source chunk's *embedded* text, so a
                    chunk about "the second option" embeds as a chunk about the second
                    option of the expense policy. The returned text is unchanged.
``both``            the two together. They do different things and a corpus can want either.

The two modes have opposite costs and opposite consequences for the index, which is why
:func:`changed_formats` reports them differently. Switching ``summary_chunk`` on adds one
point per document and recuts nothing; switching ``contextual`` on changes what every
stored vector means, so every document is stale until reindexed.

**Per-format overrides** reuse task 20's shape and its keys: a repository connector wants
summaries for the Markdown and not for the lockfiles. The configuration a document is
actually summarized under is :func:`effective`, which resolves to a leaf the way the
chunking one does.

**The organization-level default** — ``organizations.settings["summarization"]`` — carries
only the model. A connector with ``model_id: null`` uses it; an organization without one
summarizes with its distillation model; and with neither, the platform default. Same rule,
same reasons as :mod:`app.services.distillation_models`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.config import ConfigBlob
from app.services.filetypes import FORMAT_KINDS

if TYPE_CHECKING:
    from app.services.summarization_store import SummarizationHealth

#: The closed set. A string rather than an enum in the row, like ``chunk_strategy``.
SUMMARY_MODES = ("off", "summary_chunk", "contextual", "both")

Mode = Literal["off", "summary_chunk", "contextual", "both"]

#: Where the organization's default model lives inside ``organizations.settings``.
ORG_SUMMARIZATION = "summarization"

DEFAULT_MAX_SUMMARY_TOKENS = 150
DEFAULT_MAX_INPUT_TOKENS = 12_000

#: The fields whose change under ``contextual`` makes every stored vector wrong — see
#: :func:`changed_formats`. ``max_summary_tokens`` and ``max_input_tokens`` are not among
#: them: they shape the *next* summary and leave the stored ones true.
CONTEXT_TRIGGERS = ("mode", "model_id")


def adds_summary_chunk(mode: str) -> bool:
    return mode in ("summary_chunk", "both")


def prefixes_context(mode: str) -> bool:
    return mode in ("contextual", "both")


class SummarizationOverride(BaseModel):
    """A partial :class:`SummarizationConfig`, for one format kind.

    Spelled out rather than derived, for the reason
    :class:`~app.schemas.connector_config.ChunkingOverride` gives: these fields are the API.
    """

    model_config = ConfigDict(extra="ignore")

    mode: Mode | None = None
    model_id: uuid.UUID | None = None
    max_summary_tokens: int | None = Field(default=None, ge=30, le=1000)
    max_input_tokens: int | None = Field(default=None, ge=500, le=100_000)

    def changes(self) -> dict[str, Any]:
        return {
            name: value
            for name in ("mode", "model_id", "max_summary_tokens", "max_input_tokens")
            if (value := getattr(self, name)) is not None
        }


class SummarizationConfig(ConfigBlob):
    """Task 102's knobs, per connector. Off by default: it costs a model call a document."""

    mode: Mode = "off"
    #: Which model writes the summary. ``None`` is the organization's summarization
    #: default, then its distillation model, then the platform default — an id, not a
    #: name, for the reason :mod:`app.services.distillation_models` gives.
    model_id: uuid.UUID | None = None
    max_summary_tokens: int = Field(default=DEFAULT_MAX_SUMMARY_TOKENS, ge=30, le=1000)
    #: What is *sent*: the head of the document, and the tail if it fits, which is where
    #: an abstract and a conclusion live. Measured with the embedding tokenizer.
    max_input_tokens: int = Field(default=DEFAULT_MAX_INPUT_TOKENS, ge=500, le=100_000)
    #: Documents this connector may summarize in a day. ``None`` is unlimited, which is
    #: the default for the reason every other cap here defaults open: a cap nobody set
    #: should not become an outage. Counted from ``summarization_runs`` before the call.
    daily_document_cap: int | None = Field(default=None, ge=0, le=1_000_000)
    #: Per-format overrides, keyed by :data:`~app.services.filetypes.FORMAT_KINDS`.
    overrides: dict[str, SummarizationOverride] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _overrides_name_real_formats(self) -> Self:
        unknown = sorted(set(self.overrides) - set(FORMAT_KINDS))
        if unknown:
            raise ValueError(
                f"{', '.join(unknown)}: not a format this build classifies. "
                f"Available: {', '.join(FORMAT_KINDS)}."
            )
        return self

    @property
    def enabled(self) -> bool:
        """Whether *any* format under this connector is summarized."""
        return self.mode != "off" or any(
            override.mode not in (None, "off") for override in self.overrides.values()
        )


def effective(config: SummarizationConfig, kind: str) -> SummarizationConfig:
    """The configuration a document of format ``kind`` is actually summarized under.

    A leaf, with no overrides of its own, re-validated as a whole — the same contract as
    the chunking resolver, so the two screens explain themselves the same way.
    """
    override = config.overrides.get(kind)
    base = config.model_dump(mode="json")
    base.pop("overrides", None)
    if override is not None:
        base.update(override.changes())
    return SummarizationConfig.model_validate(base)


def changed_formats(before: SummarizationConfig, after: SummarizationConfig) -> frozenset[str]:
    """Which format kinds' stored *vectors* are invalidated by moving between the two.

    Only the contextual half counts. A summary chunk is one extra point added or removed
    beside source chunks that do not change, so ``off → summary_chunk`` recuts nothing;
    ``off → contextual`` changes what every chunk's vector means, and so does changing
    the model that writes the prefix while ``contextual`` is on.
    """
    changed: set[str] = set()
    for kind in set(FORMAT_KINDS) | set(before.overrides) | set(after.overrides):
        one = effective(before, kind)
        other = effective(after, kind)
        was = prefixes_context(one.mode)
        will = prefixes_context(other.mode)
        if was != will or (will and one.model_id != other.model_id):
            changed.add(kind)
    return frozenset(changed)


@dataclass(frozen=True, slots=True)
class ContextIdentity:
    """What a ``contextual`` prefix depends on, for the chunk fingerprint.

    The model's *identity* and the prompt's version — not the summary text, which is a
    per-document value the fingerprint could not know before the phase runs. A stored
    chunk was embedded with a prefix written by this model under this prompt; change
    either and the vector means something else.
    """

    model_id: uuid.UUID | None
    prompt_version: int

    def payload(self) -> dict[str, Any]:
        return {
            "model": str(self.model_id) if self.model_id else None,
            "prompt": self.prompt_version,
        }


def context_identity(
    config: SummarizationConfig, model_id: uuid.UUID | None, *, prompt_version: int
) -> ContextIdentity | None:
    """The fingerprint's summarization input under this *effective* configuration:
    something under ``contextual`` or ``both``, and nothing at all otherwise — so a
    connector that only adds summary chunks keeps the fingerprint it had."""
    if not prefixes_context(config.mode):
        return None
    return ContextIdentity(model_id=model_id, prompt_version=prompt_version)


def organization_summarization(settings: Mapping[str, Any] | None) -> SummarizationDefaults:
    """The organization's default, or the empty one. Permissive on load, like every blob
    a worker reads."""
    if not isinstance(settings, Mapping):
        return SummarizationDefaults()
    stored = settings.get(ORG_SUMMARIZATION)
    return SummarizationDefaults.load(stored if isinstance(stored, Mapping) else None)


class SummarizationDefaults(ConfigBlob):
    """``organizations.settings["summarization"]``: the model, when an organization wants
    it different from the one that distils."""

    model_id: uuid.UUID | None = None


# ---------------------------------------------------------------------------
# API bodies
# ---------------------------------------------------------------------------


class SummarizationSettingsResponse(BaseModel):
    config: SummarizationDefaults
    #: The model a connector with no choice of its own would use, after the whole chain:
    #: this section, then the distillation model, then the platform default. Null when
    #: nothing is configured anywhere, which is the state in which summarization cannot
    #: run at all.
    effective_model_id: uuid.UUID | None = None
    effective_model_name: str | None = None
    #: Where the effective model came from: ``summarization``, ``distillation`` or
    #: ``platform``. Null when there is none.
    effective_model_source: str | None = None


class SummarizationSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: uuid.UUID | None = None

    def patch(self) -> dict[str, Any]:
        """``model_fields_set`` rather than a null check: ``model_id: null`` means "back to
        the distillation model", and dropping it would make clearing impossible."""
        return {
            name: getattr(self, name)
            for name in type(self).model_fields
            if name in self.model_fields_set
        }


class SummarizationDayResponse(BaseModel):
    day: datetime
    documents: int = 0
    failures: int = 0
    capped: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


class ModelSpendResponse(BaseModel):
    model_name: str
    runs: int
    tokens_in: int
    tokens_out: int


class ConnectorSpendResponse(BaseModel):
    connector_id: uuid.UUID
    name: str | None
    documents: int
    tokens_in: int
    tokens_out: int


class WaitingConnectorResponse(BaseModel):
    connector_id: uuid.UUID
    name: str | None
    documents: int


class SummarizationHealthResponse(BaseModel):
    """The Monitoring panel's block (SPEC §10.1 as amended by task 102).

    Per day, per model, per connector, the totals, and the documents parked on a cap. The
    failure rate is computed here for the reason the memory-health one is: a denominator
    that is easy to get subtly wrong belongs in one place.
    """

    days: list[SummarizationDayResponse]
    runs: int = 0
    documents: int = 0
    failures: int = 0
    capped: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    estimated_runs: int = 0
    failure_rate: float = 0.0
    by_model: list[ModelSpendResponse]
    top_connectors: list[ConnectorSpendResponse]
    waiting: list[WaitingConnectorResponse]
    waiting_documents: int = 0

    @classmethod
    def of(cls, health: SummarizationHealth) -> SummarizationHealthResponse:
        return cls(
            days=[SummarizationDayResponse(**asdict(day)) for day in health.days],
            runs=health.runs,
            documents=health.documents,
            failures=health.failures,
            capped=health.capped,
            tokens_in=health.tokens_in,
            tokens_out=health.tokens_out,
            estimated_runs=health.estimated_runs,
            failure_rate=health.failure_rate,
            by_model=[ModelSpendResponse(**asdict(row)) for row in health.by_model],
            top_connectors=[ConnectorSpendResponse(**asdict(row)) for row in health.top_connectors],
            waiting=[WaitingConnectorResponse(**asdict(row)) for row in health.waiting],
            waiting_documents=health.waiting_documents,
        )


class SummaryEditRequest(BaseModel):
    """The operator's own summary. Replaces the model's, and is never charged to a cap."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=8000)


__all__ = [
    "CONTEXT_TRIGGERS",
    "DEFAULT_MAX_INPUT_TOKENS",
    "DEFAULT_MAX_SUMMARY_TOKENS",
    "ORG_SUMMARIZATION",
    "SUMMARY_MODES",
    "ContextIdentity",
    "Mode",
    "SummarizationConfig",
    "SummarizationDefaults",
    "SummarizationHealthResponse",
    "SummarizationOverride",
    "SummarizationSettingsRequest",
    "SummarizationSettingsResponse",
    "SummaryEditRequest",
    "adds_summary_chunk",
    "changed_formats",
    "context_identity",
    "effective",
    "organization_summarization",
    "prefixes_context",
]
