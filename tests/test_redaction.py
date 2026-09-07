"""Redaction: what it strips, what it refuses to run, and what it does when it runs out.

The pattern-safety half is the interesting one. It is a heuristic, so the tests are in two
groups on purpose — the shapes that must be refused, and the ordinary patterns an operator
will actually type, which must not be. A safety check that refuses ``\\d{4}-\\d{4}`` is
not a safety check, it is an outage with a good excuse.
"""

from __future__ import annotations

import time

import pytest

from app.core.patterns import MAX_GROUP_DEPTH, UnsafePattern, backtracking_risk, check_pattern
from app.services.redaction import MAX_FIELD_CHARS, REPLACEMENT, Redactor

# ---------------------------------------------------------------------------
# pattern safety
# ---------------------------------------------------------------------------

DANGEROUS = [
    "(a+)+",
    "(a*)*$",
    "(a|a)+",
    "(a|b)*",
    "(x+x+)+y",
    "(.*)+",
    "([a-z]+)+@",
    "(a{2,})*",
]

SAFE = [
    r"[\w.+-]+@[\w-]+\.[\w.]+",
    r"\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}",
    r"(?:sk|pk)-[A-Za-z0-9]{20,}",
    "password",
    "(secret)",
    "(abc)+",
    "a+b+",
    r"\(\d+\)+",
    "[0-9]{3}-[0-9]{2}-[0-9]{4}",
]


@pytest.mark.parametrize("pattern", DANGEROUS)
def test_a_backtracking_shape_is_refused(pattern: str) -> None:
    with pytest.raises(UnsafePattern):
        check_pattern(pattern)


@pytest.mark.parametrize("pattern", SAFE)
def test_an_ordinary_pattern_is_accepted(pattern: str) -> None:
    """The half that stops the safety check from being an outage.

    Every one of these is something somebody will paste into the Logging form — an email
    address, a card number, an API key prefix — and refusing any of them would make the
    feature unusable while looking cautious.
    """
    assert backtracking_risk(pattern) is None
    assert check_pattern(pattern) is not None


def test_a_broken_pattern_says_what_is_wrong() -> None:
    """``re`` gets there first, and its message names the position — which is more
    useful than anything the shape check could say about the same input."""
    with pytest.raises(UnsafePattern) as caught:
        check_pattern("(unclosed")

    assert "not a valid regular expression" in str(caught.value)


@pytest.mark.parametrize("pattern", ["(unclosed", "unopened)"])
def test_the_shape_check_alone_survives_unbalanced_input(pattern: str) -> None:
    """``backtracking_risk`` is public and scans raw text, so it has to answer rather
    than raise on input ``re`` would have rejected before it."""
    assert backtracking_risk(pattern) is not None


def test_an_invalid_regex_is_refused_before_the_shape_check() -> None:
    with pytest.raises(UnsafePattern) as caught:
        check_pattern("*nope")

    assert "not a valid regular expression" in str(caught.value)


def test_deep_nesting_is_refused_unread() -> None:
    with pytest.raises(UnsafePattern) as caught:
        check_pattern("(" * (MAX_GROUP_DEPTH + 1) + "a" + ")" * (MAX_GROUP_DEPTH + 1))

    assert str(MAX_GROUP_DEPTH) in str(caught.value)


def test_an_escaped_parenthesis_is_not_a_group() -> None:
    r"""``\(a+\)+`` repeats a literal bracket, not a group, so it is safe."""
    assert backtracking_risk(r"\(a+\)+") is None


def test_a_bounded_quantifier_on_a_group_is_allowed() -> None:
    """``{2,5}`` cannot blow up: the engine has at most four choices to reconsider."""
    assert backtracking_risk("(a|b){2,5}") is None


# ---------------------------------------------------------------------------
# applying
# ---------------------------------------------------------------------------


def test_nothing_configured_means_nothing_walked() -> None:
    redactor = Redactor([])

    assert redactor.active is False
    value, complete = redactor.scrub({"a": "ada@example.com"})
    assert value == {"a": "ada@example.com"}
    assert complete is True


def test_a_match_is_replaced() -> None:
    redactor = Redactor([r"[\w.+-]+@[\w-]+\.[\w.]+"])

    value, complete = redactor.scrub("write to ada@example.com please")

    assert value == f"write to {REPLACEMENT} please"
    assert complete is True


def test_every_string_in_a_nested_body_is_covered() -> None:
    """A multi-part message is a list of objects, and the text is two levels down.

    Redacting only top-level strings would leave the modern OpenAI content format —
    which is what any client sending an image uses — entirely unredacted.
    """
    redactor = Redactor(["secret"])
    body = [
        {"role": "user", "content": [{"type": "text", "text": "the secret is here"}]},
        {"role": "assistant", "content": "no secret at all"},
    ]

    value, complete = redactor.scrub(body)

    assert complete is True
    assert value[0]["content"][0]["text"] == f"the {REPLACEMENT} is here"
    assert value[1]["content"] == f"no {REPLACEMENT} at all"


def test_keys_are_left_alone() -> None:
    """Field names come from the OpenAI schema, not from the end user. Redacting one
    would produce a body no reader — and no distillation worker — can interpret."""
    redactor = Redactor(["content"])

    value, _ = redactor.scrub({"content": "keep the key"})

    assert list(value) == ["content"]


def test_non_strings_pass_through_untouched() -> None:
    redactor = Redactor(["1"])

    value, _ = redactor.scrub({"n": 123, "flag": True, "nothing": None})

    assert value == {"n": 123, "flag": True, "nothing": None}


def test_a_field_longer_than_the_cap_is_truncated() -> None:
    """The cap is what bounds the work any one pattern can be made to do."""
    redactor = Redactor(["zzz"])

    value, complete = redactor.scrub("x" * (MAX_FIELD_CHARS + 500))

    assert complete is True
    assert len(value) == MAX_FIELD_CHARS


def test_an_exhausted_budget_reports_incomplete() -> None:
    """Fail closed: the caller drops the bodies rather than storing half-redacted text."""
    redactor = Redactor(["secret"])

    value, complete = redactor.scrub("a secret", deadline=time.monotonic() - 1)

    assert complete is False
    assert value == "a secret"


def test_an_unsafe_stored_pattern_is_skipped_not_fatal() -> None:
    """A pattern written before the check existed must not stop logging altogether.

    Skipping one and applying the rest is the safe half of a bad situation; refusing to
    log at all would turn somebody's old regex into an outage of the monitoring screen.
    """
    redactor = Redactor(["(a+)+", "secret"])

    value, complete = redactor.scrub("a secret")

    assert complete is True
    assert value == f"a {REPLACEMENT}"
