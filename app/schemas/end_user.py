"""Request and response bodies for end users and their memory (SPEC §6.5, §13.1).

Two shapes are worth reading closely.

:class:`MemoryFactResponse` carries ``superseded_at`` and ``expires_at`` rather than a
single ``live`` boolean. A screen that only said "live" or "not live" could not answer the
question the memory browser exists for — *why* is this fact not being used, and when did
that happen — and the two dates are also what distinguishes a fact somebody retracted from
one that simply timed out.

:class:`MemoryFactUpdateRequest` is the one PATCH in the system where ``null`` is
meaningful, and it is meaningful for exactly one field. Sending ``expires_at: null``
clears an expiry; every other field refuses ``null`` outright rather than ignoring it, so
a client that sends one is told rather than quietly having nothing happen.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.models import MemoryFact
from app.db.models.end_user import FACT_KINDS, MAX_FACT_LENGTH
from app.services.end_user import ANON_PREFIX
from app.services.end_user_store import FactPatch
from app.services.end_users import EndUserView, FactHit

MAX_QUERY = 1000

FactText = Annotated[str, Field(min_length=1, max_length=MAX_FACT_LENGTH)]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class EndUserResponse(BaseModel):
    """One identity, with the two numbers the list screen sorts by."""

    id: uuid.UUID
    external_id: str
    label: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    request_count: int
    #: Live facts only. A count that included superseded rows would grow forever and stop
    #: meaning "how much does the assistant know about this person".
    fact_count: int
    #: Whether this identity was derived from an address rather than supplied. Shown,
    #: because an operator looking at a list of ``anon:…`` rows should be able to see at a
    #: glance that the customer's integration is not sending ``X-Gateway-User``.
    anonymous: bool

    @classmethod
    def of(cls, view: EndUserView) -> EndUserResponse:
        row = view.end_user
        return cls(
            id=row.id,
            external_id=row.external_id,
            label=row.label,
            first_seen_at=row.first_seen_at,
            last_seen_at=row.last_seen_at,
            request_count=row.request_count,
            fact_count=view.fact_count,
            anonymous=row.external_id.startswith(ANON_PREFIX),
        )


class MemoryFactResponse(BaseModel):
    id: uuid.UUID
    end_user_id: uuid.UUID
    text: str
    kind: str
    confidence: float
    source_log_id: uuid.UUID | None
    superseded_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
    last_seen_at: datetime

    @classmethod
    def of(cls, fact: MemoryFact) -> MemoryFactResponse:
        return cls(
            id=fact.id,
            end_user_id=fact.end_user_id,
            text=fact.text,
            kind=fact.kind,
            confidence=fact.confidence,
            source_log_id=fact.source_log_id,
            superseded_at=fact.superseded_at,
            expires_at=fact.expires_at,
            created_at=fact.created_at,
            last_seen_at=fact.last_seen_at,
        )


class MemoryFactCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: FactText
    kind: str = "fact"
    #: Defaults to certainty, because a person typed it. Task 13's distillation supplies
    #: the model's own number instead, and a hand-written fact should outrank a guess.
    confidence: Confidence = 1.0
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _known_kind(self) -> Self:
        if self.kind not in FACT_KINDS:
            raise ValueError(f"must be one of {', '.join(FACT_KINDS)}")
        return self


class MemoryFactUpdateRequest(BaseModel):
    """Partial. See the module docstring on ``expires_at`` and ``null``."""

    model_config = ConfigDict(extra="forbid")

    text: FactText | None = None
    kind: str | None = None
    confidence: Confidence | None = None
    expires_at: datetime | None = None
    #: Retract a fact without deleting it, or bring one back. The browser's toggle;
    #: task 13's distillation sets the same column when a newer fact contradicts an older
    #: one.
    superseded: bool | None = None

    @model_validator(mode="after")
    def _known_kind(self) -> Self:
        if self.kind is not None and self.kind not in FACT_KINDS:
            raise ValueError(f"must be one of {', '.join(FACT_KINDS)}")
        return self

    def to_patch(self, *, fields: set[str]) -> FactPatch:
        """``fields`` is the set actually present in the request body.

        Needed because ``expires_at: null`` and an omitted ``expires_at`` arrive as the
        same ``None``, and they mean opposite things — clear it, and leave it alone.
        """
        return FactPatch(
            text=self.text,
            kind=self.kind,
            confidence=self.confidence,
            expires_at=self.expires_at,
            clear_expiry="expires_at" in fields and self.expires_at is None,
            superseded=self.superseded,
        )


class MemorySearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, Field(min_length=1, max_length=MAX_QUERY)]
    limit: Annotated[int, Field(ge=1, le=50)] = 10


class MemorySearchHit(BaseModel):
    fact: MemoryFactResponse
    score: float

    @classmethod
    def of(cls, hit: FactHit) -> MemorySearchHit:
        return cls(fact=MemoryFactResponse.of(hit.fact), score=hit.score)


class MemorySearchResponse(BaseModel):
    hits: list[MemorySearchHit]
    #: Which model produced the vectors being searched. Returned for the same reason the
    #: connector debug search returns it: two searches under different embedding models
    #: are not comparable, and nothing else on the screen would say they differ.
    embedding_model: str


class MemoryPurgeResponse(BaseModel):
    facts: int
    transcripts: int


def present_fields(body: Any) -> set[str]:
    """Which keys the client actually sent. See :meth:`MemoryFactUpdateRequest.to_patch`."""
    return set(body.model_fields_set) if isinstance(body, BaseModel) else set()


__all__ = [
    "MAX_QUERY",
    "EndUserResponse",
    "MemoryFactCreateRequest",
    "MemoryFactResponse",
    "MemoryFactUpdateRequest",
    "MemoryPurgeResponse",
    "MemorySearchHit",
    "MemorySearchRequest",
    "MemorySearchResponse",
    "present_fields",
]
