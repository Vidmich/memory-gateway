"""The shape of one upstream attempt, shared by the two screens that draw it.

The gateway editor's Test panel and the monitoring drawer show the same thing — which
targets were tried, in what order, with what result — and they must show it identically,
because an operator comparing "what the test button said" with "what the log says" is
doing so precisely when something looks wrong.

The one deliberate looseness: ``extra="ignore"`` rather than the ``extra="forbid"`` used
everywhere else in ``app.schemas``. These come out of a jsonb column as well as out of
:mod:`app.services.routing`, and a row written by a build that has since added a field
must still open in the drawer. Forbidding extras on a *response* model protects nobody
and turns a schema addition into a 500 on historical data.
"""

from __future__ import annotations

import uuid
from typing import Self

from pydantic import BaseModel, ConfigDict

from app.services.routing import Attempt


class AttemptResponse(BaseModel):
    """One upstream attempt. SPEC §10.2's ``failover_attempts`` entry, as JSON."""

    model_config = ConfigDict(extra="ignore")

    target_id: uuid.UUID
    model_name: str
    status: int
    error_code: str | None = None
    latency_ms: int
    #: Whether this failure class justified another target, not whether one was tried.
    #: A ``true`` on the last attempt of a chain is how an operator learns the chain was
    #: too short rather than the error final.
    retryable: bool

    @classmethod
    def of(cls, attempt: Attempt) -> Self:
        return cls(
            target_id=attempt.target_id,
            model_name=attempt.model_name,
            status=attempt.status,
            error_code=attempt.error_code,
            latency_ms=attempt.latency_ms,
            retryable=attempt.retryable,
        )


__all__ = ["AttemptResponse"]
