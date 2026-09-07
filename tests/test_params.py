"""Generation parameters: the write-time allowlist and the request-time merge.

Both halves live in ``app/services/params.py`` and both are here, because the thing worth
protecting is the relationship between them: the allowlist is exactly the set of keys the
merge is allowed to produce, and a key that passes validation but is dropped by the merge
would be a default that silently never applies.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.errors import Validation
from app.schemas.openai import PARAMETER_FIELDS
from app.services.params import (
    MAX_STOP_LENGTH,
    MAX_STOP_SEQUENCES,
    PARAMETER_BOUNDS,
    allowed_parameters,
    resolve_params,
    validate_params,
)

# ---------------------------------------------------------------------------
# the allowlist
# ---------------------------------------------------------------------------


def test_the_allowlist_matches_the_wire_format() -> None:
    """The two lists are written in different files for different reasons — one is what a
    client may send, the other what an operator may pin — and they have to be the same
    set, or an operator can set a default the merge then throws away."""
    assert set(allowed_parameters()) == set(PARAMETER_FIELDS)


def test_nothing_is_a_valid_configuration() -> None:
    assert validate_params(None) == {}
    assert validate_params({}) == {}


def test_a_valid_set_comes_back_unchanged() -> None:
    params = {"temperature": 0.2, "top_p": 0.9, "max_tokens": 512}

    assert validate_params(params) == params


def test_an_unknown_key_is_refused_and_lists_the_alternatives() -> None:
    """A silently forwarded ``temprature`` is a default that never applies and gives no
    sign of it, which is the failure this whole function exists to remove."""
    with pytest.raises(Validation) as failure:
        validate_params({"temprature": 0.5})

    assert "temprature" in failure.value.message
    assert "temperature" in failure.value.message
    assert failure.value.param == "default_params.temprature"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("temperature", 2.5),
        ("temperature", -0.1),
        ("top_p", 1.5),
        ("presence_penalty", 3),
        ("frequency_penalty", -3),
        ("n", 0),
        ("n", 500),
        ("max_tokens", 0),
    ],
)
def test_a_value_outside_the_providers_range_is_refused(name: str, value: Any) -> None:
    with pytest.raises(Validation) as failure:
        validate_params({name: value})

    assert failure.value.param == f"default_params.{name}"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("temperature", "warm"),
        ("max_tokens", 1.5),
        ("seed", "42"),
        ("response_format", "json"),
        ("n", True),
    ],
)
def test_the_wrong_type_is_refused(name: str, value: Any) -> None:
    with pytest.raises(Validation):
        validate_params({name: value})


def test_a_boolean_is_not_accepted_as_a_number() -> None:
    """``bool`` is an ``int`` in Python, so ``n: true`` would otherwise pass the range
    check as 1 and produce a request nobody meant."""
    with pytest.raises(Validation):
        validate_params({"n": True})


@pytest.mark.parametrize("value", [0.0, 1.0, 2.0])
def test_the_boundaries_themselves_are_allowed(value: float) -> None:
    assert validate_params({"temperature": value}) == {"temperature": value}


def test_an_integer_is_accepted_where_a_float_is_expected() -> None:
    assert validate_params({"temperature": 1}) == {"temperature": 1}


def test_a_single_stop_string_is_allowed() -> None:
    assert validate_params({"stop": "\\n\\n"}) == {"stop": "\\n\\n"}


def test_too_many_stop_sequences_are_refused() -> None:
    with pytest.raises(Validation):
        validate_params({"stop": ["a"] * (MAX_STOP_SEQUENCES + 1)})


def test_an_overlong_stop_sequence_is_refused() -> None:
    with pytest.raises(Validation):
        validate_params({"stop": ["x" * (MAX_STOP_LENGTH + 1)]})


def test_a_non_string_stop_sequence_is_refused() -> None:
    with pytest.raises(Validation):
        validate_params({"stop": [1, 2]})


def test_every_bound_has_a_readable_description() -> None:
    """The message is the whole product here: "must be a number" would leave the operator
    guessing which number."""
    for name, bound in PARAMETER_BOUNDS.items():
        assert bound.description.startswith("must be"), name


# ---------------------------------------------------------------------------
# the merge
# ---------------------------------------------------------------------------


def test_the_client_wins_over_the_gateway_and_the_model() -> None:
    resolved = resolve_params(
        model_defaults={"temperature": 0.1},
        gateway_overrides={"temperature": 0.5},
        client={"temperature": 0.9},
    )

    assert resolved.values == {"temperature": 0.9}


def test_the_gateway_wins_over_the_model() -> None:
    resolved = resolve_params(
        model_defaults={"temperature": 0.1},
        gateway_overrides={"temperature": 0.5},
        client={},
    )

    assert resolved.values == {"temperature": 0.5}


def test_layers_combine_rather_than_replace() -> None:
    """A partial override overrides one key, not the whole set. Replacing would silently
    drop a model default the operator never touched."""
    resolved = resolve_params(
        model_defaults={"temperature": 0.1, "max_tokens": 100},
        gateway_overrides={"top_p": 0.8},
        client={"max_tokens": 50},
    )

    assert resolved.values == {"temperature": 0.1, "max_tokens": 50, "top_p": 0.8}


def test_missing_layers_are_treated_as_empty() -> None:
    assert resolve_params(model_defaults=None, gateway_overrides=None, client=None).values == {}


def test_the_merge_does_not_mutate_its_inputs() -> None:
    defaults = {"temperature": 0.1}

    resolve_params(model_defaults=defaults, gateway_overrides={"temperature": 0.5}, client=None)

    assert defaults == {"temperature": 0.1}
