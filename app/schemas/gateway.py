"""Request and response bodies for gateways and their API keys.

Two shapes here carry a rule that the rest of the file exists to protect.

**There is no ``slug`` on :class:`GatewayUpdateRequest`.** Not "ignored if sent" — absent,
so ``extra="forbid"`` turns an attempt into a 422 that says why. The slug is in a URL
customers have deployed; the safe way to change it is a new gateway.

**:class:`IssuedApiKeyResponse` is the only model in the codebase with a plaintext secret
on it**, and it is returned by exactly one endpoint, once. Every other key response is
:class:`ApiKeyResponse`, which has no field that could hold one.

The three config sections are sent as partial objects and merged server-side, so the
Prompt section of the editor can save without knowing what the Logging section contains.
That is what makes the editor extensible by tasks 07, 10 and 14 rather than a form that
has to send everything it has ever heard of.

**``model_id`` and ``targets`` are the same field twice**, and that is on purpose rather
than by accident. A gateway with one model is the common case and ``{"model_id": "..."}``
is how task 06's API said it; ``targets`` is the general form that failover and A/B need.
Sending both is a 422 rather than a precedence rule, because a precedence rule is a thing
somebody has to look up and get wrong once.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models import ApiKey
from app.db.models.gateway import MAX_SLUG_LENGTH, MIN_SLUG_LENGTH, ROUTING_MODES
from app.schemas.common import Page
from app.schemas.gateway_config import LimitsConfig, LoggingConfig, MemoryConfig
from app.schemas.routing import AttemptResponse
from app.services.gateway_probe import MAX_PROBE_MESSAGE, GatewayProbeResult
from app.services.gateways import (
    MAX_TARGETS,
    UNSET,
    GatewayDraft,
    GatewayPatch,
    GatewayView,
    IssuedKey,
    Maybe,
    TargetSpec,
)
from app.services.memory_preview import (
    MAX_PREVIEW_QUERY,
    CitationsPreview,
    PreviewChunk,
    PromptPreview,
    RetrievalPreview,
)

MAX_NAME = 200
MAX_DESCRIPTION = 2000
MAX_SYSTEM_CONTEXT = 32_000
MAX_KEY_NAME = 100

#: Same shape as the ``slug_is_url_safe`` CHECK and as ``_check_slug`` in the service. The
#: pattern here gives the client a rule it can enforce as you type; the service gives the
#: sentence explaining it; the constraint is the one nothing can go around.
SLUG_PATTERN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"

Slug = Annotated[
    str, Field(min_length=MIN_SLUG_LENGTH, max_length=MAX_SLUG_LENGTH, pattern=SLUG_PATTERN)
]
Name = Annotated[str, Field(min_length=1, max_length=MAX_NAME)]
Description = Annotated[str, Field(max_length=MAX_DESCRIPTION)]
SystemContext = Annotated[str, Field(max_length=MAX_SYSTEM_CONTEXT)]
KeyName = Annotated[str, Field(min_length=1, max_length=MAX_KEY_NAME)]

#: `model_id` collides with Pydantic's protected `model_` prefix. Turning the namespace
#: off is the honest fix: the field is named after the domain concept, and renaming it to
#: dodge a library convention would make the API read worse than the code.
_CONFIG = ConfigDict(extra="forbid", protected_namespaces=())


def _reject_both_target_forms(data: Any) -> Any:
    """``model_id`` and ``targets`` are two spellings of one field. Pick one."""
    if isinstance(data, dict) and data.get("model_id") is not None and data.get("targets"):
        raise ValueError(
            "Send either 'model_id' for a single target or 'targets' for a routing chain, not both."
        )
    return data


def _chain(
    targets: list[GatewayTargetRequest] | None, model_id: uuid.UUID | None
) -> tuple[TargetSpec, ...]:
    """Both spellings, as the one representation the service knows about."""
    if targets is not None:
        return tuple(target.to_spec() for target in targets)
    return (TargetSpec(model_id=model_id),) if model_id is not None else ()


def _validate_routing_mode(value: str | None) -> str | None:
    # Membership only. Whether the *chain* suits the mode — two targets for failover,
    # weights totalling 100 for A/B — is a different question, answered in the service
    # where the existing targets are visible.
    if value is not None and value not in ROUTING_MODES:
        raise ValueError(f"must be one of {', '.join(ROUTING_MODES)}")
    return value


# ---------------------------------------------------------------------------
# gateways
# ---------------------------------------------------------------------------


class GatewayTargetRequest(BaseModel):
    """One link of a routing chain. List position is priority: index 0 is tried first."""

    model_config = _CONFIG

    model_id: uuid.UUID
    #: A percentage, read only by ``ab_split``. Stored for every mode, so switching a
    #: gateway to ``single`` and back does not lose the split somebody configured.
    weight: Annotated[int, Field(ge=0, le=100)] = 100

    def to_spec(self) -> TargetSpec:
        return TargetSpec(model_id=self.model_id, weight=self.weight)


class TargetSummary(BaseModel):
    """The model a gateway routes to, as much of it as a gateway screen needs.

    Deliberately not the full ``ModelResponse``: nothing here can carry a credential
    status, so the gateway editor cannot become a second place a hint is rendered.
    """

    model_config = ConfigDict(protected_namespaces=())

    id: uuid.UUID
    name: str
    dialect: str
    enabled: bool
    #: ``None`` for a global catalog model, matching ``ModelResponse``.
    organization_id: uuid.UUID | None
    #: Position in the chain, and the A/B percentage. Both are on the *summary* rather
    #: than in a parallel array, so the editor cannot render a weight against the wrong
    #: model.
    priority: int = 0
    weight: int = 100


class GatewayResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    id: uuid.UUID
    organization_id: uuid.UUID
    slug: str
    name: str
    description: str | None
    enabled: bool
    routing_mode: str
    #: What a customer puts in their client's ``base_url``. Built server-side from
    #: ``PUBLIC_BASE_URL``, because behind an ingress the browser's origin is not it.
    endpoint_url: str
    targets: list[TargetSummary]
    system_context: str | None
    param_overrides: dict[str, Any]
    locked_params: dict[str, Any]
    #: Always complete, defaults filled in, whatever the row happens to store.
    memory_config: MemoryConfig
    logging_config: LoggingConfig
    limits: LimitsConfig
    key_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, view: GatewayView) -> Self:
        gateway = view.gateway
        return cls(
            id=gateway.id,
            organization_id=gateway.organization_id,
            slug=gateway.slug,
            name=gateway.name,
            description=gateway.description,
            enabled=gateway.enabled,
            routing_mode=gateway.routing_mode,
            endpoint_url=view.endpoint_url,
            targets=[
                TargetSummary(
                    id=target.model.id,
                    name=target.model.name,
                    dialect=target.model.dialect,
                    enabled=target.model.enabled,
                    organization_id=target.model.organization_id,
                    priority=target.priority,
                    weight=target.weight,
                )
                for target in view.targets
            ],
            system_context=gateway.system_context,
            param_overrides=dict(gateway.param_overrides or {}),
            locked_params=dict(gateway.locked_params or {}),
            memory_config=MemoryConfig.load(gateway.memory_config),
            logging_config=LoggingConfig.load(gateway.logging_config),
            limits=LimitsConfig.load(gateway.limits),
            key_count=view.key_count,
            created_at=gateway.created_at,
            updated_at=gateway.updated_at,
        )


class GatewayCreateRequest(BaseModel):
    model_config = _CONFIG

    name: Name
    slug: Slug
    description: Description | None = None
    enabled: bool = True
    routing_mode: str = "single"
    #: Optional so a gateway can exist before a model does — the editor lets you save
    #: Identity first, and the list shows "no model" rather than refusing the save.
    #: The one-target shorthand for ``targets``; see the module docstring.
    model_id: uuid.UUID | None = None
    targets: Annotated[list[GatewayTargetRequest], Field(max_length=MAX_TARGETS)] | None = None
    system_context: SystemContext | None = None
    param_overrides: dict[str, Any] = Field(default_factory=dict)
    locked_params: dict[str, Any] = Field(default_factory=dict)
    memory_config: dict[str, Any] = Field(default_factory=dict)
    logging_config: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)

    _check_routing_mode = field_validator("routing_mode")(_validate_routing_mode)

    @model_validator(mode="before")
    @classmethod
    def _one_way_of_saying_it(cls, data: Any) -> Any:
        return _reject_both_target_forms(data)

    def to_draft(self) -> GatewayDraft:
        return GatewayDraft(
            name=self.name,
            slug=self.slug,
            description=self.description,
            enabled=self.enabled,
            routing_mode=self.routing_mode,
            targets=_chain(self.targets, self.model_id),
            system_context=self.system_context,
            param_overrides=self.param_overrides,
            locked_params=self.locked_params,
            memory_config=self.memory_config,
            logging_config=self.logging_config,
            limits=self.limits,
        )


#: Fields that may be omitted but never sent as ``null``. Accepting ``{"name": null}`` and
#: changing nothing is the worst way to handle a mistake.
NOT_NULLABLE = (
    "name",
    "enabled",
    "routing_mode",
    # `model_id: null` detaches; `targets: null` would mean the same thing in a way
    # nobody would guess, so it is refused and `targets: []` is the spelling.
    "targets",
    "param_overrides",
    "locked_params",
    "memory_config",
    "logging_config",
    "limits",
)


class GatewayUpdateRequest(BaseModel):
    """Partial. Only fields actually present in the body are applied.

    ``model_id: null`` is meaningful and allowed: it detaches the gateway from its model,
    which is how you park an endpoint without deleting it.
    """

    model_config = _CONFIG

    name: Name | None = None
    description: Description | None = None
    enabled: bool | None = None
    routing_mode: str | None = None
    model_id: uuid.UUID | None = None
    targets: Annotated[list[GatewayTargetRequest], Field(max_length=MAX_TARGETS)] | None = None
    system_context: SystemContext | None = None
    param_overrides: dict[str, Any] | None = None
    locked_params: dict[str, Any] | None = None
    memory_config: dict[str, Any] | None = None
    logging_config: dict[str, Any] | None = None
    limits: dict[str, Any] | None = None

    _check_routing_mode = field_validator("routing_mode")(_validate_routing_mode)

    @model_validator(mode="before")
    @classmethod
    def _reject_slug_and_nulls(cls, data: Any) -> Any:
        data = _reject_both_target_forms(data)
        if isinstance(data, dict):
            if "slug" in data:
                # `extra="forbid"` would already refuse it; this replaces "unexpected
                # field" with the reason, which is the only part worth reading.
                raise ValueError(
                    "'slug' cannot be changed: it is part of the endpoint URL your "
                    "clients already use. Create a new gateway with the slug you want."
                )
            for name in NOT_NULLABLE:
                if name in data and data[name] is None:
                    raise ValueError(f"'{name}' cannot be null; omit it to leave it unchanged")
        return data

    def to_patch(self) -> GatewayPatch:
        """Only what was sent. ``model_fields_set`` is what makes absence expressible."""
        sent = self.model_fields_set

        def maybe[T](name: str) -> Maybe[T]:
            value: T = getattr(self, name)
            return value if name in sent else UNSET

        # Either spelling means "here is the whole chain", and neither being present
        # means "leave it alone". `model_id: null` therefore detaches, exactly as it did
        # before `targets` existed.
        chain: Maybe[tuple[TargetSpec, ...]] = (
            _chain(self.targets, self.model_id) if {"targets", "model_id"} & sent else UNSET
        )

        return GatewayPatch(
            name=maybe("name"),
            description=maybe("description"),
            enabled=maybe("enabled"),
            routing_mode=maybe("routing_mode"),
            targets=chain,
            system_context=maybe("system_context"),
            param_overrides=maybe("param_overrides"),
            locked_params=maybe("locked_params"),
            memory_config=maybe("memory_config"),
            logging_config=maybe("logging_config"),
            limits=maybe("limits"),
        )


# ---------------------------------------------------------------------------
# memory previews
# ---------------------------------------------------------------------------


class MemoryPreviewRequest(BaseModel):
    """A question to try, optionally against settings that have not been saved.

    ``memory_config`` is the same partial blob ``PATCH`` accepts and is merged the same
    way, so the tuning loop is: change a number, press Try, read the scores — without
    changing what live callers of this endpoint are getting between attempts.
    """

    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, Field(min_length=1, max_length=MAX_PREVIEW_QUERY)]
    memory_config: dict[str, Any] | None = None


class RetrievedChunkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    score: float
    text: str
    source_name: str
    page_or_section: str | None
    document_id: uuid.UUID | None
    connector_id: uuid.UUID | None
    chunk_index: int
    tokens: int
    #: Whether it survives ``doc_max_tokens``. A high-scoring chunk with ``false`` here
    #: is the screen earning its keep: the corpus is fine and the budget is the problem.
    injected: bool
    #: The ``[n]`` the prompt numbers this chunk with (task 100).
    handle: int

    @classmethod
    def of(cls, item: PreviewChunk) -> Self:
        chunk = item.chunk
        return cls(
            id=chunk.id,
            score=chunk.score,
            text=chunk.text,
            source_name=chunk.source_name,
            page_or_section=chunk.page_or_section,
            document_id=_uuid(chunk.document_id),
            connector_id=_uuid(chunk.connector_id),
            chunk_index=chunk.chunk_index,
            tokens=item.tokens,
            injected=item.injected,
            handle=item.handle,
        )


class CitationsPreviewResponse(BaseModel):
    """What a client would receive under each citation mode, for a sample answer that
    cites the first injected chunks (task 100)."""

    model_config = ConfigDict(extra="forbid")

    mode: str
    sample_answer: str
    metadata: list[dict[str, Any]]
    footer: str

    @classmethod
    def of(cls, preview: CitationsPreview) -> Self:
        return cls(
            mode=preview.mode,
            sample_answer=preview.sample_answer,
            metadata=[dict(item) for item in preview.metadata],
            footer=preview.footer,
        )


class RetrievalPreviewResponse(BaseModel):
    """What Try retrieval returns. ``outcome`` distinguishes the four kinds of empty."""

    model_config = ConfigDict(extra="forbid")

    #: What was actually embedded. Not the same as ``query`` under ``last_n_turns``, and
    #: the difference is usually what explains a surprising result.
    query: str
    outcome: str
    latency_ms: int
    error: str | None
    chunks: list[RetrievedChunkResponse]
    injected_tokens: int
    doc_max_tokens: int
    #: The tokenizer the sizes were measured with (task 101).
    tokenizer: str

    @classmethod
    def of(cls, preview: RetrievalPreview) -> Self:
        return cls(
            query=preview.query,
            outcome=preview.outcome,
            latency_ms=preview.latency_ms,
            error=preview.error,
            chunks=[RetrievedChunkResponse.of(item) for item in preview.chunks],
            injected_tokens=preview.injected_tokens,
            doc_max_tokens=preview.doc_max_tokens,
            tokenizer=preview.tokenizer,
        )


class PromptLayerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    label: str
    text: str
    tokens: int


class PromptPreviewResponse(BaseModel):
    """The assembled system message, layer by layer, with the retrieval behind it."""

    #: ``protected_namespaces`` off because ``model_name`` is the domain word here and
    #: renaming it to dodge a Pydantic convention would make the API read worse.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    layers: list[PromptLayerResponse]
    system_message: str
    total_tokens: int
    #: ``None`` when the model does not declare a window. The UI then shows token counts
    #: without a percentage, because there is nothing honest to take a percentage of.
    context_window: int | None
    model_name: str | None
    #: SPEC §7: the client messages left no room, so nothing was injected.
    overflowed: bool
    retrieval: RetrievalPreviewResponse
    citations: CitationsPreviewResponse
    #: The tokenizer every count on this screen was measured with (task 101).
    tokenizer: str

    @classmethod
    def of(cls, preview: PromptPreview) -> Self:
        return cls(
            layers=[
                PromptLayerResponse(
                    name=layer.name, label=layer.label, text=layer.text, tokens=layer.tokens
                )
                for layer in preview.layers
            ],
            system_message=preview.system_message,
            total_tokens=preview.total_tokens,
            context_window=preview.context_window,
            model_name=preview.model_name,
            overflowed=preview.overflowed,
            retrieval=RetrievalPreviewResponse.of(preview.retrieval),
            citations=CitationsPreviewResponse.of(preview.citations),
            tokenizer=preview.tokenizer,
        )


def _uuid(value: str | None) -> uuid.UUID | None:
    """A payload id, or nothing. Vector payloads are written by this system, but a
    hand-repaired point should not make a diagnostic screen 500."""
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# api keys
# ---------------------------------------------------------------------------


class ApiKeyResponse(BaseModel):
    """Everything about a key except the one thing that matters, which is gone.

    ``prefix`` is the durable display form: it carries no secret, and it is what a
    customer compares against the key in their own config when working out which one to
    revoke.
    """

    id: uuid.UUID
    gateway_id: uuid.UUID
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    expires_at: datetime | None

    @classmethod
    def of(cls, key: ApiKey) -> Self:
        return cls(
            id=key.id,
            gateway_id=key.gateway_id,
            name=key.name,
            prefix=key.prefix,
            created_at=key.created_at,
            last_used_at=key.last_used_at,
            revoked_at=key.revoked_at,
            expires_at=key.expires_at,
        )


class ApiKeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: KeyName
    #: Optional. A key that expires is one fewer credential to remember to revoke.
    expires_at: datetime | None = None


class IssuedApiKeyResponse(BaseModel):
    """The one response in this API with a live secret in it.

    Returned by ``POST /gateways/{id}/keys`` and nowhere else, because only the hash is
    stored. The client is expected to show it once with a warning and then forget it —
    reloading the page cannot bring it back, and that is the property being protected.
    """

    key: ApiKeyResponse
    token: str

    @classmethod
    def of(cls, issued: IssuedKey) -> Self:
        return cls(key=ApiKeyResponse.of(issued.key), token=issued.token)


# ---------------------------------------------------------------------------
# the probe
# ---------------------------------------------------------------------------


class GatewayTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: Annotated[str, Field(min_length=1, max_length=MAX_PROBE_MESSAGE)]


class PromptMessageResponse(BaseModel):
    role: str
    content: str


class GatewayTestResponse(BaseModel):
    """What the probe saw: the prompt, the answer, and where the time went.

    Not an error envelope. "The upstream said 401" is the successful answer to "does this
    gateway work", and the UI renders it in red itself.
    """

    ok: bool
    total_ms: int
    upstream_ms: int
    assembled_prompt: list[PromptMessageResponse]
    model_name: str | None = None
    content: str | None = None
    upstream_status: int | None = None
    error_message: str | None = None
    locked_overrides: list[str] = Field(default_factory=list)
    #: Only when more than one target was tried. A green tick on a gateway whose primary
    #: is dead is worse than a red one, so the editor renders this list next to it.
    attempts: list[AttemptResponse] = Field(default_factory=list)

    model_config = ConfigDict(protected_namespaces=())

    @classmethod
    def of(cls, result: GatewayProbeResult) -> Self:
        return cls(
            ok=result.ok,
            total_ms=result.total_ms,
            upstream_ms=result.upstream_ms,
            assembled_prompt=[
                PromptMessageResponse(role=message.role, content=message.content)
                for message in result.assembled_prompt
            ],
            model_name=result.model_name,
            content=result.content,
            upstream_status=result.upstream_status,
            error_message=result.error_message,
            locked_overrides=list(result.locked_overrides),
            attempts=[AttemptResponse.of(attempt) for attempt in result.attempts],
        )


GatewayPage = Page[GatewayResponse]


__all__ = [
    "SLUG_PATTERN",
    "ApiKeyCreateRequest",
    "ApiKeyResponse",
    "GatewayCreateRequest",
    "GatewayPage",
    "GatewayResponse",
    "GatewayTargetRequest",
    "GatewayTestRequest",
    "GatewayTestResponse",
    "GatewayUpdateRequest",
    "IssuedApiKeyResponse",
    "TargetSummary",
]
