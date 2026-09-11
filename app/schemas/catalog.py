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
from app.schemas.platform import EffectiveTokenizerResponse
from app.services.catalog import (
    UNSET,
    Maybe,
    ModelCalibration,
    ModelDraft,
    ModelPatch,
    ModelView,
    tokenizer_of,
)
from app.services.model_probe import ProbeResult
from app.services.tokenizers import (
    DERIVATIONS,
    DRIFT_WARNING,
    FALLBACK,
    MAX_RATIO,
    MIN_RATIO,
    TOKENIZER_NAMES,
    TokenizerSpec,
)

#: Bounds chosen so a mistyped value is a 422 on the form rather than a row that makes
#: every later read expensive. None of them is a provider limit — those live in
#: ``app.services.params``.
MAX_NAME = 200
MAX_DESCRIPTION = 2000
MAX_URL = 2000
MAX_SYSTEM_CONTEXT = 8000
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 600
#: A context window has to be big enough to hold a prompt and small enough to be a real
#: number. The ceiling is generous — the largest published windows are around 2M tokens —
#: because the wrong failure here is refusing a value a provider actually offers.
MIN_CONTEXT_WINDOW = 256
MAX_CONTEXT_WINDOW = 10_000_000

Name = Annotated[str, Field(min_length=1, max_length=MAX_NAME)]
BaseUrl = Annotated[str, Field(min_length=1, max_length=MAX_URL)]
ModelId = Annotated[str, Field(min_length=1, max_length=MAX_NAME)]
Timeout = Annotated[int, Field(ge=MIN_TIMEOUT_SECONDS, le=MAX_TIMEOUT_SECONDS)]
ContextWindow = Annotated[int, Field(ge=MIN_CONTEXT_WINDOW, le=MAX_CONTEXT_WINDOW)]
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
    #: ``None`` means the window is not known, not that it is unlimited — see the column's
    #: docstring. The gateway's overflow guard is skipped for such a model.
    context_window: int | None
    #: Task 101. The stored override, or ``None`` for *derived*; and what is actually in
    #: effect, with its origin. Both, the way task 20 returns ``effective_chunking``: the
    #: form shows the derived value greyed until somebody overrides it.
    tokenizer: TokenizerSpec | None
    effective_tokenizer: EffectiveTokenizerResponse
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
            context_window=model.context_window,
            tokenizer=TokenizerSpec.model_validate(model.tokenizer) if model.tokenizer else None,
            effective_tokenizer=EffectiveTokenizerResponse.of(tokenizer_of(model)),
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
    context_window: ContextWindow | None = None
    #: Task 101. Omitted or ``null`` derives the tokenizer from the dialect and model id.
    tokenizer: TokenizerSpec | None = None
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
            context_window=self.context_window,
            tokenizer=self.tokenizer,
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
    #: Genuinely nullable, unlike the fields in ``NOT_NULLABLE``: sending ``null`` is how
    #: an operator says "I no longer claim to know this model's window", which switches
    #: the overflow guard back off.
    context_window: ContextWindow | None = None
    #: Nullable in the same sense: ``null`` removes the override and the model goes back
    #: to derivation, which the response then says.
    tokenizer: TokenizerSpec | None = None
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
            context_window=maybe("context_window"),
            tokenizer=maybe("tokenizer"),
            enabled=maybe("enabled"),
        )


class CalibrationResponse(BaseModel):
    """Task 101: our count against the provider's, for one model.

    ``ratio`` is provider ÷ ours over the window — ``1.04`` reads "we undercount by four
    percent". ``proposed`` is the ``approximate`` ratio **Calibrate** would store, present
    only when the tokenizer is approximate and there is something to calibrate from.
    """

    model_id: uuid.UUID
    tokenizer: EffectiveTokenizerResponse
    estimated: int
    reported: int
    samples: int
    ratio: float | None
    #: Beyond :data:`DRIFT_WARNING` — the model page and the gateway show a warning.
    warns: bool
    proposed: TokenizerSpec | None

    @classmethod
    def of(cls, entry: ModelCalibration) -> Self:
        calibration = entry.calibration
        return cls(
            model_id=entry.model_id,
            tokenizer=EffectiveTokenizerResponse.of(entry.tokenizer),
            estimated=calibration.estimated if calibration else 0,
            reported=calibration.reported if calibration else 0,
            samples=calibration.samples if calibration else 0,
            ratio=calibration.ratio if calibration else None,
            warns=calibration.warns if calibration else False,
            proposed=entry.proposed,
        )


class DerivationResponse(BaseModel):
    """One row of the derivation table, for the form to match while somebody types."""

    dialect: str | None
    prefix: str
    spec: TokenizerSpec


class TokenizersResponse(BaseModel):
    """The closed registry and the derivation table (task 101). Served rather than
    duplicated in the web bundle, so the two cannot disagree."""

    names: list[str]
    derivations: list[DerivationResponse]
    fallback: TokenizerSpec
    min_ratio: float
    max_ratio: float
    drift_warning: float

    @classmethod
    def current(cls) -> Self:
        return cls(
            names=list(TOKENIZER_NAMES),
            derivations=[
                DerivationResponse(dialect=row.dialect, prefix=row.prefix, spec=row.spec)
                for row in DERIVATIONS
            ],
            fallback=FALLBACK,
            min_ratio=MIN_RATIO,
            max_ratio=MAX_RATIO,
            drift_warning=DRIFT_WARNING,
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
