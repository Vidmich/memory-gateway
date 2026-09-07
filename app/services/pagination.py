"""Cursor pagination, shared by every list endpoint.

SPEC §12.2 makes all of them cursor-paginated, and the reason is worth stating: an offset
page over a table that is being written to shows some rows twice and skips others, which
on the request-log screen — read while traffic is arriving — looks exactly like a bug in
the gateway.

Primary keys are UUIDv7, so ``id`` already sorts by creation time. That makes the cursor
just an id — "everything before this one" — served by the primary-key index with no
sort, no offset, and no second column to keep in step.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.core.errors import Validation

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of results, in the shape SPEC §12.2 specifies."""

    items: tuple[T, ...]
    next_cursor: str | None


def decode_cursor(cursor: str | None) -> uuid.UUID | None:
    """A cursor is the opaque form of the last id the client saw.

    Malformed values are a 422 rather than an ignored parameter: silently returning page
    one when the client asked for page four is the kind of failure that gets diagnosed as
    data loss.
    """
    if cursor is None or cursor == "":
        return None
    try:
        return uuid.UUID(cursor)
    except ValueError as exc:
        raise Validation("Invalid pagination cursor.", param="cursor") from exc


def page_of[T](rows: Sequence[T], *, limit: int, cursor_of: Callable[[T], uuid.UUID]) -> Page[T]:
    """Trim an over-fetched result into a page.

    Callers ask the database for ``limit + 1`` rows; the extra one is how "is there
    another page" is answered without a second ``COUNT``, which on a large table costs
    more than the page itself.
    """
    if len(rows) > limit:
        kept = tuple(rows[:limit])
        return Page(items=kept, next_cursor=str(cursor_of(kept[-1])))
    return Page(items=tuple(rows), next_cursor=None)


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(limit, MAX_PAGE_SIZE))
