"""Deciding whether a regular expression is safe to run on untrusted input.

Pure analysis, no I/O, so it can be called from a Pydantic validator — which is the point:
the operator typing a redaction pattern into the gateway's Logging form is the last moment
anybody can be told about it cheaply. :mod:`app.services.redaction` is what actually runs
the patterns, in the log flusher, over whatever a customer's end users typed.

Catastrophic backtracking needs two things at once: a group that can match the same input
more than one way, and an unbounded quantifier on that group. ``(a+)+`` has both, and so
does ``(a|b)+`` — a one-character alternation still offers the engine a choice at every
position. Looking for the *pair* is what keeps this from refusing every pattern with a
``+`` in it.

It is a heuristic and it is deliberately conservative, because the two mistakes are not
symmetric. Refusing a safe pattern costs an operator a rewrite —  ``(ab|cd)+`` spelled as
``(?:ab|cd)+``, or as a character class. Accepting an unsafe one costs a background worker
that stops making progress, on input the operator does not control, with no way to
interrupt it: Python's ``re`` does not release the GIL or check for cancellation while it
is matching, so there is no timeout to fall back on.
"""

from __future__ import annotations

import re

#: Groups nested deeper than this are refused unread. Depth is where this analysis gets
#: expensive and where human review gets unreliable, and no pattern for an email address
#: or a card number needs it.
MAX_GROUP_DEPTH = 5


class UnsafePattern(ValueError):
    """A pattern that compiles but must not be run against untrusted input."""


def check_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a pattern, refusing the ones that are unsafe to run.

    Raises :class:`UnsafePattern` — a ``ValueError`` — so the configuration schema in
    :mod:`app.schemas.gateway_config` surfaces it as a 422 on the field the operator is
    typing into, rather than as an incident three days later.
    """
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise UnsafePattern(f"'{pattern}' is not a valid regular expression: {exc}") from exc

    problem = backtracking_risk(pattern)
    if problem is not None:
        raise UnsafePattern(f"'{pattern}' {problem}")
    return compiled


def backtracking_risk(pattern: str) -> str | None:
    """Why this pattern is dangerous, or ``None`` if it looks fine.

    Catastrophic backtracking needs two things: a group that can match the same input in
    more than one way, and an unbounded quantifier on that group. ``(a+)+`` has both;
    ``(a|b)+`` has both, because a one-character alternation still gives the engine a
    choice at every position. Detecting the *pair* is what keeps this from refusing every
    pattern with a ``+`` in it.

    Conservative on purpose. It will refuse some safe patterns, and the cost of that is
    an operator rewriting ``(ab|cd)+`` as ``(?:ab|cd)+`` or spelling it differently. The
    cost of the opposite mistake is a flusher that stops flushing.
    """
    depth = 0
    for index, char in enumerate(pattern):
        if _escaped(pattern, index):
            continue
        if char == "(":
            depth += 1
            if depth > MAX_GROUP_DEPTH:
                return f"nests groups more than {MAX_GROUP_DEPTH} deep, which is refused unread"
            body = _group_body(pattern, index)
            if body is None:
                return "has an unbalanced '('"
            after = index + len(body) + 2
            if not _unbounded_quantifier(pattern, after):
                continue
            if _has_unbounded_quantifier(body):
                return (
                    "repeats a group that itself repeats, like '(a+)+'. That backtracks "
                    "exponentially; rewrite it without the inner repetition."
                )
            if _has_alternation(body):
                return (
                    "repeats a group containing an alternation, like '(a|b)+'. That "
                    "backtracks exponentially; use a character class or a non-capturing "
                    "group with distinct prefixes."
                )
        elif char == ")":
            depth -= 1

    if depth != 0:
        return "has unbalanced parentheses"
    return None


def _escaped(pattern: str, index: int) -> bool:
    """True when the character at ``index`` is preceded by an odd number of backslashes."""
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and pattern[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _group_body(pattern: str, start: int) -> str | None:
    """The text between the parenthesis at ``start`` and its match, or ``None``."""
    depth = 0
    for index in range(start, len(pattern)):
        if _escaped(pattern, index):
            continue
        if pattern[index] == "(":
            depth += 1
        elif pattern[index] == ")":
            depth -= 1
            if depth == 0:
                return pattern[start + 1 : index]
    return None


def _unbounded_quantifier(pattern: str, index: int) -> bool:
    """Whether an unbounded repetition starts at ``index``.

    ``{2,5}`` is bounded and safe; ``{2,}`` is not. ``?`` is bounded by definition.
    """
    if index >= len(pattern):
        return False
    char = pattern[index]
    if char in "*+":
        return True
    if char == "{":
        end = pattern.find("}", index)
        if end == -1:
            return False
        return pattern[index + 1 : end].endswith(",")
    return False


def _has_unbounded_quantifier(body: str) -> bool:
    for index, char in enumerate(body):
        if _escaped(body, index):
            continue
        if char in "*+" or (char == "{" and _unbounded_quantifier(body, index)):
            return True
    return False


def _has_alternation(body: str) -> bool:
    """A ``|`` at the top level of this group, outside a character class."""
    depth = 0
    in_class = False
    for index, char in enumerate(body):
        if _escaped(body, index):
            continue
        if in_class:
            if char == "]":
                in_class = False
            continue
        if char == "[":
            in_class = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "|" and depth == 0:
            return True
    return False


__all__ = ["MAX_GROUP_DEPTH", "UnsafePattern", "backtracking_risk", "check_pattern"]
