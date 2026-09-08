"""The audit screen and its export.

A thin service over :mod:`app.services.audit_store`: paging, and turning the same page
into CSV. The two things worth explaining are both about the export.

**It streams, and it is capped.** A CSV of an organization's whole history is built one
page at a time and yielded as it goes, so a large export costs one page of memory rather
than all of it. The cap is :data:`MAX_EXPORT_ROWS`, and it is reported *inside the file*:
a response whose body has already started cannot change its status code, so the honest
options were a trailing marker row or lying by omission. The marker has an empty ``id``,
which is what tells a reader it is not an event.

**The diff is one column.** Flattening it into columns is not possible — every event
changes different paths — and one CSV row per change would make the row count disagree
with the event count, which is the number somebody exported the file to check. So the
changes are one cell, one line each, in the same ``path: before → after`` form the screen
shows.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

from app.core.errors import Validation
from app.core.tenancy import Actor
from app.db.models import AuditEvent
from app.db.models.audit import MAX_ACTION_LENGTH, TARGET_TYPES
from app.services.audit_store import AuditFilters, AuditStore
from app.services.pagination import Page, clamp_limit, decode_cursor, page_of
from app.services.rate_limit import FixedWindowLimiter

#: The most rows one export contains. Ten thousand is a spreadsheet somebody can actually
#: open, and several years of a busy organization's configuration changes.
MAX_EXPORT_ROWS = 10_000

#: How many rows are fetched per round trip while streaming. Large enough that a full
#: export is a handful of queries, small enough that one page is not a memory event.
EXPORT_PAGE_SIZE = 500

#: The columns, in order. Chosen so the file is readable without the API: who, when, what,
#: to what, and from where.
EXPORT_COLUMNS = (
    "id",
    "created_at",
    "actor",
    "actor_type",
    "actor_user_id",
    "action",
    "target_type",
    "target_id",
    "target",
    "changes",
    "ip",
    "user_agent",
    "request_id",
)

TRUNCATION_NOTICE = (
    f"(truncated: this export stopped at the {MAX_EXPORT_ROWS:,}-row cap. "
    f"Narrow the filter — by action, by target, or by date — to see the rest.)"
)


def build_audit_filters(
    *,
    organization_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> AuditFilters:
    """Turn query parameters into a checked filter.

    No default window, unlike the request log's: this table is small and the question
    asked of it is "when did this change", where defaulting to the last day would hide
    the answer. The two validations are the ones a typo produces — an unknown target type
    and a backwards range — and both are refused rather than returning an empty list,
    which is indistinguishable from "nothing happened".
    """
    if target_type is not None and target_type not in TARGET_TYPES:
        raise Validation(
            f"Unknown target type. One of: {', '.join(sorted(TARGET_TYPES))}.",
            param="target_type",
        )
    if action is not None and len(action) > MAX_ACTION_LENGTH:
        raise Validation("That action name is too long to be one.", param="action")

    begin = _aware(start)
    finish = _aware(end)
    if begin is not None and finish is not None and finish <= begin:
        raise Validation("'to' has to be after 'from'.", param="to")

    return AuditFilters(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action=action.strip() or None if action else None,
        target_type=target_type,
        target_id=target_id,
        start=begin,
        end=finish,
    )


def _aware(moment: datetime | None) -> datetime | None:
    """A naive timestamp means UTC, which is what the API documents and the UI sends."""
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


class AuditService:
    def __init__(self, store: AuditStore, *, export_limiter: FixedWindowLimiter | None = None):
        self._store = store
        self._limiter = export_limiter

    async def list_events(
        self,
        actor: Actor,
        filters: AuditFilters,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> Page[AuditEvent]:
        size = clamp_limit(limit)
        after = decode_cursor(cursor)
        async with self._store.begin(actor.scope) as reader:
            rows = await reader.events(filters, after=after, limit=size)
        return page_of(rows, limit=size, cursor_of=lambda row: row.id)

    async def export(self, actor: Actor, filters: AuditFilters) -> AsyncIterator[str]:
        """Check the ceiling, then hand back the stream.

        Deliberately not a generator itself. An export is the cheapest way to pull an
        organization's entire configuration history, and repeating it in a loop is the one
        access pattern on this screen worth a ceiling — but a ``429`` raised from inside a
        generator arrives after the response has already started with a ``200``, which no
        client can act on. So the check happens here, before there is a response, and the
        generator below is what streams.

        The check lives in the service rather than in the router so a second caller of
        this method cannot skip it.
        """
        if self._limiter is not None:
            await self._limiter.check(str(actor.user_id))
        return self._stream(actor, filters)

    async def _stream(self, actor: Actor, filters: AuditFilters) -> AsyncIterator[str]:
        """CSV for the current filter, a page at a time."""
        yield _row(EXPORT_COLUMNS)

        written = 0
        after: uuid.UUID | None = None
        while written < MAX_EXPORT_ROWS:
            want = min(EXPORT_PAGE_SIZE, MAX_EXPORT_ROWS - written)
            async with self._store.begin(actor.scope) as reader:
                rows = await reader.events(filters, after=after, limit=want)
            page = page_of(rows, limit=want, cursor_of=lambda row: row.id)
            if not page.items:
                return

            yield "".join(_row(_columns(event)) for event in page.items)
            written += len(page.items)
            if page.next_cursor is None:
                return
            after = page.items[-1].id

        # Reached the cap with more to come. See the module docstring on why this is a
        # row in the file rather than an error.
        yield _row(("", "", "", "", "", TRUNCATION_NOTICE, "", "", "", "", "", "", ""))


def _columns(event: AuditEvent) -> Sequence[str]:
    return (
        str(event.id),
        event.created_at.isoformat() if event.created_at else "",
        event.actor_label or "",
        event.actor_type,
        str(event.actor_user_id) if event.actor_user_id else "",
        event.action,
        event.target_type,
        str(event.target_id) if event.target_id else "",
        event.target_label or "",
        _changes(event),
        event.ip or "",
        event.user_agent or "",
        event.request_id or "",
    )


def _changes(event: AuditEvent) -> str:
    """One line per changed path, inside a single cell.

    ``path: before -> after`` rather than the raw JSON: a spreadsheet cell holding a
    document is a cell nobody reads, and this is the form the screen shows too.
    """
    payload = event.diff or {}
    lines = []
    for change in payload.get("changes", []):
        if not isinstance(change, dict):
            continue
        before = change.get("before", "-")
        after = change.get("after", "-")
        lines.append(f"{change.get('path', '?')}: {before} -> {after}")
    summary = payload.get("summary")
    if summary:
        lines.append(f"summary: {summary}")
    omitted = payload.get("omitted")
    if omitted:
        lines.append(f"(+{omitted} more fields)")
    return "\n".join(lines)


def _row(values: Sequence[str]) -> str:
    """One CSV record, quoted by the standard library rather than by hand.

    ``\\r\\n`` is RFC 4180's line ending and what Excel expects; the writer is given a
    fresh buffer per record because the alternative — one shared buffer — makes this
    function depend on being called in order from one task.
    """
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerow(list(values))
    return buffer.getvalue()


__all__ = [
    "EXPORT_COLUMNS",
    "EXPORT_PAGE_SIZE",
    "MAX_EXPORT_ROWS",
    "TRUNCATION_NOTICE",
    "AuditService",
    "build_audit_filters",
]
