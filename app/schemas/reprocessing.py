"""Request and response bodies for reprocessing runs (task 104)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.models import ReprocessingRun
from app.db.models.reprocessing import REPROCESSING_SCOPES
from app.services.connector_store import ReprocessScope
from app.services.filetypes import FORMAT_KINDS


class ReprocessRequest(BaseModel):
    """Which documents to reprocess. The default is the stale ones, which is the reason
    the run exists: reprocessing current documents is a waste the old endpoint could not
    avoid."""

    model_config = ConfigDict(extra="forbid")

    scope: str = "stale"
    #: Required under ``scope: formats``, ignored otherwise.
    formats: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _known(self) -> Self:
        if self.scope not in REPROCESSING_SCOPES:
            raise ValueError(
                f"'{self.scope}' is not a scope. Available: {', '.join(REPROCESSING_SCOPES)}."
            )
        unknown = sorted(set(self.formats) - set(FORMAT_KINDS))
        if unknown:
            raise ValueError(
                f"{', '.join(unknown)}: not a format this build classifies. "
                f"Available: {', '.join(FORMAT_KINDS)}."
            )
        if self.scope == "formats" and not self.formats:
            raise ValueError("Name at least one format under scope 'formats'.")
        return self

    def to_scope(self) -> ReprocessScope:
        return ReprocessScope(kind=self.scope, formats=frozenset(self.formats))


class ReprocessingProgress(BaseModel):
    done: int
    total: int
    fraction: float
    eta_seconds: int | None


def progress_of(run: ReprocessingRun, *, now: datetime | None = None) -> ReprocessingProgress:
    """Documents settled over documents claimed, with an ETA at the observed rate — task
    17's arithmetic, in documents rather than points, so both screens read alike. No ETA
    before anything has settled, after the run finished, or without an elapsed second:
    a number extrapolated from nothing is worse than a blank."""
    total = run.total
    done = run.settled
    fraction = done / total if total else 1.0
    if run.finished_at is not None or done <= 0 or done >= total:
        return ReprocessingProgress(done=done, total=total, fraction=fraction, eta_seconds=None)
    elapsed = ((now or datetime.now(UTC)) - run.started_at).total_seconds()
    if elapsed <= 0:
        return ReprocessingProgress(done=done, total=total, fraction=fraction, eta_seconds=None)
    rate = done / elapsed
    return ReprocessingProgress(
        done=done, total=total, fraction=fraction, eta_seconds=int((total - done) / rate)
    )


class ReprocessingRunResponse(BaseModel):
    id: uuid.UUID
    connector_id: uuid.UUID
    #: What made the documents stale: ``chunking``, ``embedding_model``, ``tokenizer``,
    #: ``summarization``, ``extractor``, or ``manual`` for a run over documents that were
    #: not.
    trigger: str
    scope: str
    formats: list[str]
    requested_by: uuid.UUID | None
    requested_by_label: str | None
    #: The platform reindex that spawned this run, when one did.
    reindex_run_id: uuid.UUID | None
    #: ``running``, ``succeeded``, ``partial`` (finished with failures) or ``failed``.
    status: str
    total: int
    done: int
    failed: int
    skipped: int
    estimated_tokens: int
    spent_tokens: int
    error: str | None
    resumed: int
    report: dict[str, Any]
    progress: ReprocessingProgress
    started_at: datetime
    finished_at: datetime | None
    #: True in the response to the request that created the run; false when the request
    #: found one already going and returned it instead.
    created: bool = True

    @classmethod
    def of(cls, run: ReprocessingRun, *, created: bool = True) -> ReprocessingRunResponse:
        progress = progress_of(run)
        return cls(
            id=run.id,
            connector_id=run.connector_id,
            trigger=run.trigger,
            scope=run.scope,
            formats=list(run.formats or []),
            requested_by=run.requested_by,
            requested_by_label=run.requested_by_label,
            reindex_run_id=run.reindex_run_id,
            status=run.status,
            total=run.total,
            done=run.done,
            failed=run.failed,
            skipped=run.skipped,
            estimated_tokens=run.estimated_tokens,
            spent_tokens=run.spent_tokens,
            error=run.error,
            resumed=run.resumed,
            report=dict(run.report or {}),
            progress=progress,
            started_at=run.started_at,
            finished_at=run.finished_at,
            created=created,
        )


class ReprocessingRunList(BaseModel):
    items: list[ReprocessingRunResponse]


class StaleAlertResponse(BaseModel):
    connector_id: uuid.UUID
    connector_name: str
    stale_documents: int
    stale_since: datetime
    age_hours: float


class StaleAlertList(BaseModel):
    items: list[StaleAlertResponse]


class StalePreviewResponse(BaseModel):
    """What a save of the given patch would mark stale, per format, before saving."""

    #: Indexed documents per format kind the change would invalidate. Empty when the
    #: patch changes nothing that reaches the index.
    formats: dict[str, int]
    total: int


__all__ = [
    "ReprocessRequest",
    "ReprocessingProgress",
    "ReprocessingRunList",
    "ReprocessingRunResponse",
    "StaleAlertList",
    "StaleAlertResponse",
    "StalePreviewResponse",
    "progress_of",
]
