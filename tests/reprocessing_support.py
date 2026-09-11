"""Builders for reprocessing tests (task 104)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.ids import uuid7
from app.db.models import Connector, ReprocessingRun


def make_reprocessing_run(
    connector: Connector,
    *,
    status: str = "succeeded",
    total: int = 3,
    done: int | None = None,
    failed: int = 0,
    skipped: int = 0,
    trigger: str = "chunking",
    scope: str = "stale",
    reindex_run_id: uuid.UUID | None = None,
    **overrides: Any,
) -> ReprocessingRun:
    """A run row without running anything — for the cross-tenant net, which needs a
    *foreign* id that is real, and for the store contract."""
    started = datetime.now(UTC) - timedelta(minutes=5)
    finished = None if status == "running" else datetime.now(UTC)
    values: dict[str, Any] = {
        "id": uuid7(),
        "organization_id": connector.organization_id,
        "connector_id": connector.id,
        "trigger": trigger,
        "scope": scope,
        "formats": [],
        "requested_by": None,
        "requested_by_label": "tests",
        "reindex_run_id": reindex_run_id,
        "status": status,
        "total": total,
        "done": (total - failed - skipped) if done is None and status != "running" else (done or 0),
        "failed": failed,
        "skipped": skipped,
        "estimated_tokens": 0,
        "spent_tokens": 0,
        "error": None,
        "resumed": 0,
        "report": {},
        "started_at": started,
        "finished_at": finished,
    }
    values.update(overrides)
    return ReprocessingRun(**values)


__all__ = ["make_reprocessing_run"]
