"""Redacting request bodies before they are persisted.

SPEC §10.2 lets an organization list regular expressions that are stripped from bodies
"before persistence, never after". That word matters: a redaction applied on read is a
display filter, and the raw card number is still in the backup, in the replica, and in
the export. This module is the only place bodies are transformed, and it runs inside the
log flusher, which is upstream of every write.

**The threat is the pattern itself.** An operator can type ``(a+)+$`` into a form, and
Python's ``re`` will then spend geological time on a forty-character input. There is no
way to interrupt a running ``re`` match from another coroutine — the C loop does not
release the GIL and does not check for cancellation — so "apply with a timeout" is not
available to us and pretending otherwise would be worse than saying so. Three real
defences instead, in the order they fire:

1. :func:`app.core.patterns.check_pattern` refuses the shapes that backtrack
   catastrophically, at save time, on the form the operator is looking at. It is a
   heuristic, deliberately conservative: a nested unbounded quantifier is refused whether
   or not this particular one is exponential.
2. Inputs are capped at :data:`MAX_FIELD_CHARS`. Backtracking cost is a function of input
   length, so a bound on the input is a bound on the damage a pattern that slipped
   through can do to one field.
3. A wall-clock budget is checked *between* patterns and *between* fields. It cannot stop
   a match that is already running, but it stops the batch from compounding: one slow
   pattern costs one field, not the whole flush.

**Failure is closed.** If the budget runs out mid-record, the body is not stored at all —
it is dropped and the row is marked ``redaction_budget``. Storing a partially redacted
body would be the one outcome worse than storing nothing, because it looks redacted.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.core.patterns import UnsafePattern, check_pattern

logger = logging.getLogger(__name__)

#: What a match is replaced with. Fixed rather than configurable: the value is a marker,
#: and a configurable one is a way to make redacted text look like real text.
REPLACEMENT = "[redacted]"

#: Longest string one pattern is run over. Longer values are truncated *for matching
#: purposes only* — the tail is kept, unredacted, only when no pattern is configured.
MAX_FIELD_CHARS = 100_000

#: Wall clock for the whole of one record's redaction. Generous: a sane pattern set over
#: a normal prompt is microseconds, so anything approaching this is already pathological.
DEFAULT_BUDGET_SECONDS = 0.25


class Redactor:
    """Applies a compiled pattern set within a time budget.

    Holds compiled patterns rather than strings because a flush batch shares one
    redactor across every record for a gateway, and recompiling per record would be the
    most expensive thing in the write path by an order of magnitude.
    """

    def __init__(
        self,
        patterns: Iterable[str],
        *,
        budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    ) -> None:
        self._patterns: list[re.Pattern[str]] = []
        for pattern in patterns:
            try:
                self._patterns.append(check_pattern(pattern))
            except UnsafePattern:
                # Stored before this check existed, or written by a route that bypassed
                # validation. Skipping one pattern is the safe half of a bad choice: the
                # remaining ones still apply, and the alternative — refusing to log at
                # all — turns a bad regex into an outage of the monitoring screen.
                logger.warning("skipping an unsafe redaction pattern", extra={"pattern": pattern})
        self._budget = budget_seconds

    @property
    def active(self) -> bool:
        """False when nothing is configured, so the caller can skip the walk entirely."""
        return bool(self._patterns)

    def scrub(self, value: Any, *, deadline: float | None = None) -> tuple[Any, bool]:
        """Redact every string inside ``value``. Returns ``(result, complete)``.

        Walks lists and mappings so a multi-part message — ``[{"type": "text", "text":
        ...}]`` — is covered as thoroughly as a plain string one. Keys are left alone:
        they are field names from the OpenAI schema, not user content, and redacting one
        would produce a body no reader could interpret.
        """
        if not self._patterns:
            return value, True
        if deadline is None:
            deadline = time.monotonic() + self._budget
        return self._walk(value, deadline)

    def _walk(self, value: Any, deadline: float) -> tuple[Any, bool]:
        if time.monotonic() > deadline:
            return value, False

        if isinstance(value, str):
            return self._apply(value, deadline)
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in value.items():
                scrubbed, ok = self._walk(item, deadline)
                if not ok:
                    return value, False
                result[str(key)] = scrubbed
            return result, True
        if isinstance(value, Sequence) and not isinstance(value, str | bytes):
            items: list[Any] = []
            for item in value:
                scrubbed, ok = self._walk(item, deadline)
                if not ok:
                    return value, False
                items.append(scrubbed)
            return items, True
        return value, True

    def _apply(self, text: str, deadline: float) -> tuple[str, bool]:
        # Truncating first bounds the work every pattern below does. A body longer than
        # this is a document paste, and storing 100 000 characters of it redacted is a
        # better outcome than storing all of it unredacted or none of it at all.
        head = text[:MAX_FIELD_CHARS]
        for pattern in self._patterns:
            if time.monotonic() > deadline:
                return text, False
            head = pattern.sub(REPLACEMENT, head)
        return head, True


__all__ = [
    "DEFAULT_BUDGET_SECONDS",
    "MAX_FIELD_CHARS",
    "REPLACEMENT",
    "Redactor",
]
