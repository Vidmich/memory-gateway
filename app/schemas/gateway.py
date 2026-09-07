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
from app.services.gateway_probe import MAX_PROBE_MESSAGE, GatewayProbeResult
from app.services.gateways import UNSET, GatewayDraft, GatewayPatch, GatewayView, IssuedKey, Maybe

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


def _validate_routing_mode(value: str | None) -> str | None:
    # Membership only. Whether the mode has a working *implementation* is a different
    # question, answered in the service so task 08 changes one place.
    if value is not None and value not in ROUTING_MODES:
        raise ValueError(f"must be one of {', '.join(ROUTING_MODES)}")
    return value


# ---------------------------------------------------------------------------
# gateways
# ---------------------------------------------------------------------------


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
                    id=model.id,
                    name=model.name,
                    dialect=model.dialect,
                    enabled=model.enabled,
                    organization_id=model.organization_id,
                )
                for model in view.models
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
    model_id: uuid.UUID | None = None
    system_context: SystemContext | None = None
    param_overrides: dict[str, Any] = Field(default_factory=dict)
    locked_params: dict[str, Any] = Field(default_factory=dict)
    memory_config: dict[str, Any] = Field(default_factory=dict)
    logging_config: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)

    _check_routing_mode = field_validator("routing_mode")(_validate_routing_mode)

    def to_draft(self) -> GatewayDraft:
        return GatewayDraft(
            name=self.name,
            slug=self.slug,
            description=self.description,
            enabled=self.enabled,
            routing_mode=self.routing_mode,
            model_id=self.model_id,
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

        return GatewayPatch(
            name=maybe("name"),
            description=maybe("description"),
            enabled=maybe("enabled"),
            routing_mode=maybe("routing_mode"),
            model_id=maybe("model_id"),
            system_context=maybe("system_context"),
            param_overrides=maybe("param_overrides"),
            locked_params=maybe("locked_params"),
            memory_config=maybe("memory_config"),
            logging_config=maybe("logging_config"),
            limits=maybe("limits"),
        )


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
        )


GatewayPage = Page[GatewayResponse]


__all__ = [
    "SLUG_PATTERN",
    "ApiKeyCreateRequest",
    "ApiKeyResponse",
    "GatewayCreateRequest",
    "GatewayPage",
    "GatewayResponse",
    "GatewayTestRequest",
    "GatewayTestResponse",
    "GatewayUpdateRequest",
    "IssuedApiKeyResponse",
    "TargetSummary",
]
