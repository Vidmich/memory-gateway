"""Request and response bodies for upstream models.

The one shape worth reading closely is the credential. It goes **in** as a plain string
on create and update, and comes **out** only as :class:`CredentialStatus` —
``{"configured": true, "hint": "sk-...4f2a"}``, which is SPEC §5.4 verbatim. There is no
response model anywhere in this file with a field that could hold the value, which is a
stronger guarantee than remembering to exclude it: adding one would be a visible edit to
a class whose docstring says not to.

``PATCH`` distinguishes three states per field — absent, ``null``, and a value — because
"leave the credential alone" and "remove the credential" are different requests and a
two-state model cannot say both. Absent is the default; ``null`` clears; anything else
replaces. Fields where ``null`` is meaningless (a model must have a name) refuse it
outright rather than ignoring it, so a caller who sends one is told instead of quietly
getting nothing.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models.upstream_model import AUTH_TYPES, DEFAULT_TIMEOUT_SECONDS, DIALECTS, SCOPES
from app.services.catalog import UNSET, Maybe, ModelDraft, ModelPatch, ModelView
from app.services.model_probe import ProbeResult

#: Bounds chosen so a mistyped value is a 422 on the form rather than a row that makes
#: every later read expensive. None of them is a provider limit — those live in
#: ``app.services.params``.
MAX_NAME = 200
MAX_DESCRIPTION = 2000
MAX_URL = 2000
MAX_SYSTEM_CONTEXT = 8000
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 600

Name = Annotated[str, Field(min_length=1, max_length=MAX_NAME)]
BaseUrl = Annotated[str, Field(min_length=1, max_length=MAX_URL)]
ModelId = Annotated[str, Field(min_length=1, max_length=MAX_NAME)]
Timeout = Annotated[int, Field(ge=MIN_TIMEOUT_SECONDS, le=MAX_TIMEOUT_SECONDS)]
SystemContext = Annotated[str, Field(max_length=MAX_SYSTEM_CONTEXT)]
Description = Annotated[str, Field(max_length=MAX_DESCRIPTION)]

#: The credential is bounded but not otherwise validated: providers issue keys in every
#: shape, and a format check here would reject the next one somebody invents.
Credential = Annotated[str, Field(min_length=1, max_length=4096)]


def _validate_dialect(value: str | None) -> str | None:
    # Membership only. Whether the dialect has a *working adapter* is a different
    # question, answered in the service so task 16 changes one place.
    if value is not None and value not in DIALECTS:
        raise ValueError(f"must be one of {', '.join(DIALECTS)}")
    return value


def _validate_auth_type(value: str | None) -> str | None:
    if value is not None and value not in AUTH_TYPES:
        raise ValueError(f"must be one of {', '.join(AUTH_TYPES)}")
    return value


def _validate_scope(value: str) -> str:
    if value not in SCOPES:
        raise ValueError(f"must be one of {', '.join(SCOPES)}")
    return value


def _validate_base_url(value: str | None) -> str | None:
    if value is None:
        return value
    if not value.startswith(("http://", "https://")):
        raise ValueError("must be an http:// or https:// URL")
    if "#" in value:
        # A fragment is never sent over the wire, so one here is always a paste mistake
        # that would silently change the effective URL.
        raise ValueError("must not contain a '#' fragment")
    return value.rstrip("/")


class CredentialStatus(BaseModel):
    """SPEC §5.4: what a response may say about a stored secret, and no more.

    ``hint`` can be ``None`` while ``configured`` is ``True`` — for a row written before
    hints were stored, and for a global model being read by someone who does not own it.
    """

    configured: bool
    hint: str | None = None


class ModelResponse(BaseModel):
    id: uuid.UUID
    #: ``None`` for a global model. Present so a client can tell the two apart without
    #: string-matching on ``scope``.
    organization_id: uuid.UUID | None
    scope: str
    name: str
    description: str | None
    base_url: str
    dialect: str
    upstream_model_id: str
    auth_type: str
    credential: CredentialStatus
    extra_headers: dict[str, str]
    system_context: str | None
    default_params: dict[str, Any]
    timeout_seconds: int
    enabled: bool
    #: Whether *this* caller may change it. Answered by the server so the UI and the API
    #: cannot disagree about who owns a row.
    editable: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, view: ModelView) -> Self:
        model = view.model
        return cls(
            id=model.id,
            organization_id=model.organization_id,
            scope=model.scope,
            name=model.name,
            description=model.description,
            base_url=model.base_url,
            dialect=model.dialect,
            upstream_model_id=model.upstream_model_id,
            auth_type=model.auth_type,
            credential=CredentialStatus(
                configured=model.credential_ciphertext is not None,
                hint=view.credential_hint,
            ),
            extra_headers=view.extra_headers,
            system_context=model.system_context,
            default_params=dict(model.default_params or {}),
            timeout_seconds=model.timeout_seconds,
            enabled=model.enabled,
            editable=view.editable,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class ModelCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    base_url: BaseUrl
    upstream_model_id: ModelId
    dialect: str = "openai"
    auth_type: str = "bearer"
    credential: Credential | None = None
    description: Description | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)
    system_context: SystemContext | None = None
    default_params: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: Timeout = DEFAULT_TIMEOUT_SECONDS
    enabled: bool = True
    #: ``org`` by default. Writing ``global`` needs ``platform:administer``, which the
    #: service checks — the route is open to anyone with ``resources:write``.
    scope: str = "org"

    _check_url = field_validator("base_url")(_validate_base_url)
    _check_dialect = field_validator("dialect")(_validate_dialect)
    _check_auth_type = field_validator("auth_type")(_validate_auth_type)
    _check_scope = field_validator("scope")(_validate_scope)

    def to_draft(self) -> ModelDraft:
        return ModelDraft(
            name=self.name,
            base_url=self.base_url,
            upstream_model_id=self.upstream_model_id,
            dialect=self.dialect,
            auth_type=self.auth_type,
            credential=self.credential,
            description=self.description,
            extra_headers=self.extra_headers,
            system_context=self.system_context,
            default_params=self.default_params,
            timeout_seconds=self.timeout_seconds,
            enabled=self.enabled,
            scope=self.scope,
        )


#: Fields that may be omitted but never sent as ``null``. A model with no name or no base
#: URL is not a thing, so ``{"name": null}`` is a mistake — and the worst way to handle a
#: mistake is to accept the request and change nothing.
NOT_NULLABLE = (
    "name",
    "base_url",
    "dialect",
    "upstream_model_id",
    "auth_type",
    "extra_headers",
    "default_params",
    "timeout_seconds",
    "enabled",
)


class ModelUpdateRequest(BaseModel):
    """Partial. Only fields actually present in the body are applied."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    base_url: BaseUrl | None = None
    upstream_model_id: ModelId | None = None
    dialect: str | None = None
    auth_type: str | None = None
    #: The three-state field: absent keeps, ``null`` clears, a string replaces.
    credential: Credential | None = None
    description: Description | None = None
    extra_headers: dict[str, str] | None = None
    system_context: SystemContext | None = None
    default_params: dict[str, Any] | None = None
    timeout_seconds: Timeout | None = None
    enabled: bool | None = None

    _check_url = field_validator("base_url")(_validate_base_url)
    _check_dialect = field_validator("dialect")(_validate_dialect)
    _check_auth_type = field_validator("auth_type")(_validate_auth_type)

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_nulls(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for name in NOT_NULLABLE:
                if name in data and data[name] is None:
                    raise ValueError(f"'{name}' cannot be null; omit it to leave it unchanged")
        return data

    def to_patch(self) -> ModelPatch:
        """Only what was sent. ``model_fields_set`` is what makes absence expressible."""
        sent = self.model_fields_set

        def maybe[T](name: str) -> Maybe[T]:
            value: T = getattr(self, name)
            return value if name in sent else UNSET

        return ModelPatch(
            name=maybe("name"),
            description=maybe("description"),
            base_url=maybe("base_url"),
            dialect=maybe("dialect"),
            upstream_model_id=maybe("upstream_model_id"),
            auth_type=maybe("auth_type"),
            credential=maybe("credential"),
            extra_headers=maybe("extra_headers"),
            system_context=maybe("system_context"),
            default_params=maybe("default_params"),
            timeout_seconds=maybe("timeout_seconds"),
            enabled=maybe("enabled"),
        )


class ModelTestRequest(ModelCreateRequest):
    """An unsaved draft to probe.

    The same body as create, so the form can send exactly what it is about to save. It
    inherits ``scope`` too, which the probe ignores — a connection does not care who owns
    the row it will become.
    """


class ProbeResponse(BaseModel):
    """The four things the button reports. Not an error envelope: "the upstream said 401"
    is the successful answer to "does this work", and the UI renders it in red itself."""

    ok: bool
    latency_ms: int
    upstream_status: int | None = None
    error_message: str | None = None
    model_echo: str | None = None

    @classmethod
    def of(cls, result: ProbeResult) -> Self:
        return cls(
            ok=result.ok,
            latency_ms=result.latency_ms,
            upstream_status=result.upstream_status,
            error_message=result.error_message,
            model_echo=result.model_echo,
        )
