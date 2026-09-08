"""Response shapes for the Audit log screen and the contextual history tabs.

Two decisions worth stating.

**A change carries its kind.** The stored diff distinguishes "this field did not exist"
from "this field held null" by *omitting* a key, which is exactly right in JSONB and
exactly wrong in a typed response — a client would have to inspect key presence to tell
``null → "***"`` (a credential being set on a model that had one field) from
``(absent) → "***"`` (a header being added). So the wire form names it: ``added``,
``removed`` or ``changed``, one field the UI can switch on.

**The actor is a name, not a join.** ``actor`` is the email as it was at the time, read
straight off the row, so a screen listing two hundred events makes no lookups and an event
whose actor has since been removed still says who it was.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict

from app.db.models import AuditEvent

_CONFIG = ConfigDict(extra="forbid")

type ChangeKind = Literal["added", "removed", "changed"]


class AuditChangeResponse(BaseModel):
    """One field that moved, at a dotted path — ``memory_config.doc_top_k``."""

    model_config = _CONFIG

    path: str
    kind: ChangeKind
    before: Any = None
    after: Any = None
    #: The value was longer than the diff stores and is shown cut. Sent so the UI can say
    #: so, rather than letting somebody read a truncated prompt as the whole one.
    truncated: bool = False

    @classmethod
    def of(cls, change: dict[str, Any]) -> Self:
        has_before = "before" in change
        has_after = "after" in change
        kind: ChangeKind = "changed"
        if not has_before:
            kind = "added"
        elif not has_after:
            kind = "removed"
        return cls(
            path=str(change.get("path", "")),
            kind=kind,
            before=change.get("before"),
            after=change.get("after"),
            truncated=bool(change.get("truncated", False)),
        )


class AuditEventResponse(BaseModel):
    """One row of the audit log."""

    model_config = _CONFIG

    id: uuid.UUID
    created_at: datetime
    organization_id: uuid.UUID | None = None
    actor_user_id: uuid.UUID | None = None
    #: The actor's email at the time, or the job name for a ``system`` event.
    actor: str | None = None
    actor_type: str
    action: str
    target_type: str
    target_id: uuid.UUID | None = None
    #: The target's human name at the time, so a deleted gateway is still a slug.
    target: str | None = None
    changes: list[AuditChangeResponse]
    #: How many changed fields did not fit. Zero almost always; sent so a save that
    #: rewrote a whole configuration does not silently look smaller than it was.
    omitted: int = 0
    #: A bulk operation's count and examples — a resync, an upload, a purge.
    summary: dict[str, Any] | None = None
    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None

    @classmethod
    def of(cls, event: AuditEvent) -> Self:
        payload = event.diff or {}
        raw = payload.get("changes") or []
        return cls(
            id=event.id,
            created_at=event.created_at,
            organization_id=event.organization_id,
            actor_user_id=event.actor_user_id,
            actor=event.actor_label,
            actor_type=event.actor_type,
            action=event.action,
            target_type=event.target_type,
            target_id=event.target_id,
            target=event.target_label,
            changes=[AuditChangeResponse.of(change) for change in raw if isinstance(change, dict)],
            omitted=int(payload.get("omitted") or 0),
            summary=payload.get("summary"),
            ip=str(event.ip) if event.ip else None,
            user_agent=event.user_agent,
            request_id=event.request_id,
        )


__all__ = ["AuditChangeResponse", "AuditEventResponse", "ChangeKind"]
