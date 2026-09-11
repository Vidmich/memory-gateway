"""The platform's own configuration, as a schema (task 17, SPEC §15.3).

Everything an operator can change without a deploy lives in one blob with named sections,
and each section is stored as one row in ``platform_settings``. Two shapes rather than
one, deliberately: the *schema* is what validates a change and what the screen renders,
and the *rows* are what carry attribution — "who raised the storage cap" is a question
somebody eventually asks, and a single-row table could not answer it.

The sections reuse the types they replace. ``logging`` is the very
:class:`~app.schemas.gateway_config.LoggingConfig` a gateway stores, and the two rate-limit
sections are the :class:`~app.schemas.gateway_config.Quota` the limiter already enforces —
so a platform default cannot express something a gateway cannot hold, and the ceiling
cannot be compared against a differently-shaped number.

**Ceilings are maxima, not defaults.** ``retention`` bounds how long an organization may
keep data, and the direction is one-way on purpose: an org may always be stricter than the
platform, never more permissive. That is the shape a data-processing agreement has, and
inverting it by accident is the kind of bug that is discovered in an audit.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.config import ConfigBlob
from app.schemas.gateway_config import LoggingConfig, Quota
from app.services.tokenizers import Effective, TokenizerSpec, effective

#: Section names, which are also the ``platform_settings.key`` values. Derived from the
#: model below rather than repeated, so a section cannot be added to one and not the other.
#: (Defined after :class:`PlatformSettings`.)


class EmbeddingChoice(BaseModel):
    """SPEC §9.4's platform embedding model — provider, model and width, together.

    One section rather than three settings because the three are only ever correct as a
    set: a model changed without its dimension produces vectors Qdrant refuses, and a
    dimension changed without its model produces vectors it accepts and cannot rank. A
    single row makes the change atomic and makes "what is the platform embedding on"
    a single value to compare a collection against.
    """

    model_config = ConfigDict(extra="ignore")

    provider: Literal["openai", "hash"] = "hash"
    #: The provider's model id. Free text: a self-hosted OpenAI-compatible endpoint can
    #: be serving anything, and an allowlist here would be a list to maintain forever.
    name: str = Field(default="hash-bow", min_length=1, max_length=200)
    dimension: int = Field(default=256, ge=8, le=8192)
    #: Task 101. What ``chunk_size`` is measured with. ``None`` derives it from the
    #: provider and model name — ``cl100k_base`` for ``text-embedding-3-*``, an
    #: approximation for anything we do not ship a vocabulary for. This is the one
    #: tokenizer setting that *changes chunking*: it is part of every document's chunk
    #: fingerprint, and a change to it marks every document stale.
    tokenizer: TokenizerSpec | None = None

    def same_as(self, other: EmbeddingChoice) -> bool:
        """Whether a change to ``other`` would need a reindex.

        The provider is *not* part of it. Moving the same model between two
        OpenAI-compatible endpoints — a self-hosted deployment, a different region —
        produces the same vectors, and forcing a re-embed of the whole corpus for that
        would be an expensive way to change a base URL.

        Neither is the tokenizer: it changes how documents are *cut*, not how their text
        embeds, so it invalidates chunks (through the fingerprint) without invalidating a
        collection. That is a recut of what is stale, on the connectors' own terms, not a
        platform re-embed of everything.
        """
        return self.name == other.name and self.dimension == other.dimension

    def effective_tokenizer(self) -> Effective:
        """Derived from the provider and model unless overridden — the same rule an
        upstream model follows, so the two screens read the same way."""
        return effective(self.provider, self.name, self.tokenizer)


class RetentionCeilings(BaseModel):
    """The longest an organization may keep each kind of data.

    ``None`` means the platform sets no ceiling, which is the default: a limit nobody
    configured should not silently start deleting a customer's logs on upgrade.
    """

    model_config = ConfigDict(extra="ignore")

    max_body_days: int | None = Field(default=None, ge=1, le=3650)
    max_metadata_days: int | None = Field(default=None, ge=1, le=3650)

    @model_validator(mode="after")
    def _bodies_within_metadata(self) -> Self:
        if (
            self.max_body_days is not None
            and self.max_metadata_days is not None
            and self.max_body_days > self.max_metadata_days
        ):
            # The same rule LoggingConfig enforces one level down. A ceiling pair that
            # cannot be satisfied by any valid gateway configuration is not a policy,
            # it is a form that refuses every save with a message about a different field.
            raise ValueError(
                "the body-retention ceiling cannot exceed the metadata-retention ceiling"
            )
        return self

    def cap_body(self, days: int) -> int:
        return min(days, self.max_body_days) if self.max_body_days is not None else days

    def cap_metadata(self, days: int) -> int:
        return min(days, self.max_metadata_days) if self.max_metadata_days is not None else days


class StorageCaps(BaseModel):
    """SPEC §9.2's per-file cap and the per-organization quota."""

    model_config = ConfigDict(extra="ignore")

    max_file_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    quota_bytes: int | None = Field(default=None, ge=1024)


class LimitPolicy(BaseModel):
    """Two quotas with opposite jobs.

    ``defaults`` is what a *new* gateway starts from — a starting point somebody can then
    lower or raise. ``global_model_ceilings`` is what a gateway pointed at a model on the
    operator's own credential may never exceed, whatever it asks for. Keeping them in one
    section makes the difference visible on the screen where both are set, which is the
    only place the distinction is easy to get wrong.
    """

    model_config = ConfigDict(extra="ignore")

    defaults: Quota = Field(default_factory=Quota)
    global_model_ceilings: Quota = Field(default_factory=Quota)


class DistillationDefaults(BaseModel):
    """The platform's fallback distillation model (SPEC §6.4).

    An upstream-model id, which has to name a *global* model since every organization
    uses it. Validated where it is written rather than here — this schema cannot reach the
    catalog, and a settings module that could would be a settings module with a database.
    """

    model_config = ConfigDict(extra="ignore")

    model_id: uuid.UUID | None = None


class PlatformSettings(ConfigBlob):
    """Every operator-configurable value, in sections.

    A :class:`~app.schemas.config.ConfigBlob` so that a section added by a later build
    loads against an older row, and so that ``merge_config`` gives PATCH its partial-update
    semantics and its refusal of unknown keys for free.
    """

    embedding: EmbeddingChoice = Field(default_factory=EmbeddingChoice)
    distillation: DistillationDefaults = Field(default_factory=DistillationDefaults)
    #: The full defaults a new gateway inherits, not a partial: this *is* the base that
    #: an organization's own partial defaults are merged over.
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    retention: RetentionCeilings = Field(default_factory=RetentionCeilings)
    limits: LimitPolicy = Field(default_factory=LimitPolicy)
    storage: StorageCaps = Field(default_factory=StorageCaps)


#: The stored keys, derived from the model so the two cannot disagree.
SECTIONS: tuple[str, ...] = tuple(
    name for name in PlatformSettings.model_fields if name != "version"
)


# ---------------------------------------------------------------------------
# API shapes
# ---------------------------------------------------------------------------


class SettingAttribution(BaseModel):
    """When a section was last changed and by whom. Absent while it still comes from the
    environment, which is how the screen distinguishes "bootstrapped" from "configured"."""

    key: str
    updated_at: datetime
    updated_by: uuid.UUID | None = None
    updated_by_label: str | None = None


class PlatformSettingsResponse(BaseModel):
    settings: PlatformSettings
    #: One entry per section that has a row. A section missing from this list is running
    #: on its environment-variable bootstrap.
    attribution: list[SettingAttribution] = Field(default_factory=list)
    #: Sections whose value came from an environment variable rather than the database.
    #: Named explicitly rather than inferred from the list above, because "not configured"
    #: and "configured to exactly the bootstrap value" are different states.
    from_environment: list[str] = Field(default_factory=list)
    #: The run a change to the embedding model started, while it is still going.
    reindex: ReindexRunResponse | None = None
    #: The model that run is moving to. ``settings.embedding`` still names the old one on
    #: purpose — until the aliases swap, the old one is what every collection agrees with,
    #: and a screen showing the new one would be describing a state that does not exist
    #: yet. See :mod:`app.services.reindex`.
    pending_embedding: EmbeddingChoice | None = None
    #: Task 101. The tokenizer chunking measures with right now, resolved: what the
    #: embedding section shows beside its greyed-out derived value. Filled by the route,
    #: because it is a fact about the process (a vocabulary that failed to load says so
    #: here) rather than about the stored row.
    embedding_tokenizer: EffectiveTokenizerResponse | None = None


class EffectiveTokenizerResponse(BaseModel):
    """A resolved tokenizer and where it came from (task 101). Shared by the model page
    and the platform embedding section, so the two read identically."""

    #: The stored form, for the form's override fields.
    spec: TokenizerSpec
    #: ``derived`` or ``override``.
    origin: str
    #: What the tokenizer says it is — ``o200k_base``, ``approximate:3.5``, or
    #: ``words (cl100k_base unavailable)`` when a vocabulary failed to load.
    name: str
    #: One line for the screen: ``o200k_base (derived)``.
    label: str
    #: True exactly when ``name`` is not what ``spec`` asked for.
    degraded: bool
    #: True for ``approximate`` — the one kind a calibration can move.
    approximate: bool

    @classmethod
    def of(cls, resolved: Effective) -> EffectiveTokenizerResponse:
        return cls(
            spec=resolved.spec,
            origin=resolved.origin,
            name=resolved.name,
            label=resolved.label(),
            degraded=resolved.degraded,
            approximate=resolved.approximate,
        )


class PlatformSettingsPatch(BaseModel):
    """A partial update. Only the sections present are touched."""

    model_config = ConfigDict(extra="forbid")

    embedding: dict[str, Any] | None = None
    distillation: dict[str, Any] | None = None
    logging: dict[str, Any] | None = None
    retention: dict[str, Any] | None = None
    limits: dict[str, Any] | None = None
    storage: dict[str, Any] | None = None
    #: Required when the patch changes the embedding model or dimension. Not a boolean
    #: "yes I am sure" — the collections and the estimated cost are shown first, and this
    #: is the operator repeating the model name they were shown. A change of this size
    #: should be impossible to make by accident with a stray PATCH.
    confirm_reindex: str | None = None

    def sections(self) -> dict[str, dict[str, Any]]:
        return {
            name: value
            for name, value in self.model_dump(exclude_none=True).items()
            if name != "confirm_reindex" and isinstance(value, dict)
        }


class ReindexEstimate(BaseModel):
    """What a reindex would cost, before anybody starts one.

    ``points`` and ``tokens`` are counted rather than guessed — the chunks are in the
    index with their text — so this is an estimate only in the sense that a provider's
    tokenizer may differ from ours by a few percent.

    The recut figures are the exception and are deliberately *not* folded into the token
    count. Nothing here knows how many chunks re-chunking a document under a new model will
    produce, and a number invented for that would be the one an operator anchored on.
    """

    collections: list[str] = Field(default_factory=list)
    organizations: int = 0
    points: int = 0
    tokens: int = 0
    from_model: str | None = None
    to_model: str
    to_dimension: int
    #: Connectors whose chunk boundaries came out of the embedding model and therefore have
    #: to be *recut* from object storage rather than re-embedded from the index — task 20's
    #: ``semantic`` strategy. Counted separately because it is a different kind of cost:
    #: object reads, extraction and re-chunking, none of which the token figure above
    #: covers. An operator deciding whether to change the platform model needs to see it
    #: before they decide, not discover it from the run's duration.
    recut_connectors: int = 0
    recut_documents: int = 0


class ReindexRequest(BaseModel):
    """A rebuild, of the whole platform or of one organization.

    There is deliberately no ``connector_id``. A reindex re-embeds chunks that are still
    correct under a different model, and an embedding model is a property of the whole
    *collection* — one connector re-embedded on its own would leave the tenant's index
    holding vectors from two models, which is the exact failure SPEC §9.4 makes the model a
    platform-level setting to prevent. The operation a connector actually needs after a
    chunking change is a re-*ingestion*, which is
    ``POST /api/v1/connectors/{id}/reindex``.
    """

    model_config = ConfigDict(extra="forbid")

    organization_id: uuid.UUID | None = None
    #: Report the estimate and start nothing. The default, because SPEC §13.2 asks for
    #: confirmation on destructive actions and this one is merely expensive — but an
    #: expensive action started by a curious POST is the same surprise.
    dry_run: bool = False


class ReindexTargetResponse(BaseModel):
    organization_id: uuid.UUID
    organization_name: str | None = None
    collection: str
    status: str
    total_points: int
    done_points: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    @property
    def fraction(self) -> float:
        return self.done_points / self.total_points if self.total_points else 0.0


class ReindexRunResponse(BaseModel):
    id: uuid.UUID
    scope: str
    organization_id: uuid.UUID | None = None
    status: str
    from_model: str | None = None
    from_dimension: int | None = None
    to_model: str
    to_dimension: int
    estimated_points: int = 0
    estimated_tokens: int = 0
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    targets: list[ReindexTargetResponse] = Field(default_factory=list)
    #: Seconds remaining at the rate observed so far, or ``None`` before enough of the
    #: work has finished for a rate to mean anything. A number invented from two points
    #: is worse than an honest blank on a screen somebody is deciding to wait on.
    eta_seconds: int | None = None


class MaintenanceRunResponse(BaseModel):
    id: uuid.UUID
    job: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    report: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class PartitionRunway(BaseModel):
    """How many days of partitions exist ahead of today, per table."""

    table: str
    days_ahead: int
    last_day: str | None = None
    #: Below the alert threshold. A field rather than a comparison the UI redoes, so the
    #: screen and the metric cannot disagree about when to go red.
    low: bool = False


class MaintenanceResponse(BaseModel):
    runway: list[PartitionRunway] = Field(default_factory=list)
    runway_threshold_days: int
    last_runs: list[MaintenanceRunResponse] = Field(default_factory=list)
    reindex: ReindexRunResponse | None = None
    recent_reindexes: list[ReindexRunResponse] = Field(default_factory=list)


class SweepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Nothing is deleted without this. The default is a report, and it is the default
    #: because the first version of a sweeper is usually wrong in one direction and the
    #: wrong direction here is unrecoverable customer data loss.
    apply: bool = False
    organization_id: uuid.UUID | None = None


class OrphanGroup(BaseModel):
    store: str
    kind: str
    count: int = 0
    #: A handful of identifiers, so an operator can go and look at one before allowing
    #: the destructive pass. Bounded, because a report is not a data export.
    sample: list[str] = Field(default_factory=list)


class SweepResponse(BaseModel):
    applied: bool
    organizations: int = 0
    groups: list[OrphanGroup] = Field(default_factory=list)
    deleted: int = 0


class ErasureStore(BaseModel):
    """What was removed from one store, named so the report reads as a checklist."""

    store: str
    removed: int = 0
    #: What was looked at, when "removed 0" needs to be distinguished from "did not look".
    checked: bool = True


class ErasureReport(BaseModel):
    """SPEC §6.5's artefact: the thing you hand somebody who asks whether a deletion
    request was honoured."""

    subject: str
    subject_id: uuid.UUID
    organization_id: uuid.UUID
    requested_at: datetime
    stores: list[ErasureStore] = Field(default_factory=list)
    #: True only when every store reported and none failed. A partial erasure that
    #: claimed completeness would be the worst possible artefact.
    complete: bool = True


class RetentionCeilingsResponse(BaseModel):
    """What the platform allows, for a screen inside an organization.

    Its own response rather than a slice of the settings document, because this is the
    one part of the platform configuration an organization is *entitled* to see: it
    explains why the number they typed is not the number being honoured. Everything else
    on that screen — other tenants' quotas, the operator's spending ceilings, the
    embedding model — is none of their business.
    """

    max_body_days: int | None = None
    max_metadata_days: int | None = None

    @property
    def set(self) -> bool:
        return self.max_body_days is not None or self.max_metadata_days is not None


class OrganizationDeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The organization's slug, typed by the operator. SPEC §13.2's typed confirmation,
    #: enforced by the API rather than only by the form.
    confirm: str
    #: Days before the destructive pass runs. Zero is allowed and is the escape hatch for
    #: a customer who has asked for immediate deletion in writing.
    grace_days: Annotated[int, Field(ge=0, le=90)] = 7


class VectorBindingResponse(BaseModel):
    """Where one organization's vectors are, and whether they are moving."""

    organization_id: uuid.UUID
    backend: str
    status: str
    #: Only set while a migration is in flight, and only then is it meaningful: reads go to
    #: ``backend`` throughout, which is the whole zero-downtime property.
    target: str | None = None
    #: The live physical collection, for a backend that cannot answer that about itself.
    #: Null for Qdrant, whose alias is authoritative.
    collection: str | None = None


class VectorBackendsResponse(BaseModel):
    """What this deployment offers, and where everybody is.

    ``enabled`` is read-only on purpose and there is no field here for a URL. A backend's
    address is deployment topology, configured in the environment; a settings screen able
    to point one somewhere new would put a server address a tenant's data flows to behind
    a form, which is the surface task 18 closed for upstream models.
    """

    enabled: list[str]
    default: str
    bindings: list[VectorBindingResponse]


class VectorMigrationRequest(BaseModel):
    """Move one organization's vectors to another backend.

    ``dry_run`` returns the plan and starts nothing, which is what the confirmation dialog
    calls — so the numbers an operator agrees to are the ones this endpoint counted.
    """

    backend: str
    dry_run: bool = False


class VectorMigrationResponse(BaseModel):
    """What a migration would move, or has begun moving.

    Points and facts rather than a cost estimate, which is the difference from a reindex:
    document chunks are copied rather than re-embedded, so the number that matters is how
    long it takes rather than what it costs.
    """

    organization_id: uuid.UUID
    source: str
    target: str
    target_collection: str
    points: int
    facts: int
    started: bool


# Declared after the models it refers to, because ``PlatformSettingsResponse`` names
# ``ReindexRunResponse`` and Python has read neither by the time the first class body runs.
PlatformSettingsResponse.model_rebuild()


__all__ = [
    "SECTIONS",
    "DistillationDefaults",
    "EmbeddingChoice",
    "ErasureReport",
    "ErasureStore",
    "LimitPolicy",
    "MaintenanceResponse",
    "MaintenanceRunResponse",
    "OrganizationDeletionRequest",
    "OrphanGroup",
    "PartitionRunway",
    "PlatformSettings",
    "PlatformSettingsPatch",
    "PlatformSettingsResponse",
    "ReindexEstimate",
    "ReindexRequest",
    "ReindexRunResponse",
    "ReindexTargetResponse",
    "RetentionCeilings",
    "RetentionCeilingsResponse",
    "SettingAttribution",
    "StorageCaps",
    "SweepRequest",
    "SweepResponse",
    "VectorBackendsResponse",
    "VectorBindingResponse",
    "VectorMigrationRequest",
    "VectorMigrationResponse",
]
