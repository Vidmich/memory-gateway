"""Generation parameters: what an operator may set, and how the layers combine.

Two halves. :func:`validate_params` is the write-time gate on operator-authored
configuration; :func:`resolve_params` is the request-time merge. They live together
because the allowlist below is exactly the set of keys the merge is allowed to produce.

The merge order, lowest precedence first:

1. the upstream model's ``default_params`` — sensible values for that provider,
2. the gateway's ``param_overrides``   — the organisation's house style for this endpoint,
3. the client's request                — the caller's explicit intent.

The client winning is deliberate for v1: an override is a *default*, not a cap. Task 06
adds ``locked_params``, which is the mechanism for pinning a value regardless of what the
client asks for — and it plugs in at the marked line below rather than by reordering this
merge.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.errors import Validation


def resolve_params(
    *,
    model_defaults: Mapping[str, Any] | None,
    gateway_overrides: Mapping[str, Any] | None,
    client: Mapping[str, Any] | None,
) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    resolved.update(model_defaults or {})
    resolved.update(gateway_overrides or {})
    resolved.update(client or {})
    # Task 06: re-apply the gateway's locked params here, after the client's values.
    return resolved


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
#
# `default_params` and (from task 06) `param_overrides` are operator-authored JSON that
# ends up on the wire to a provider. Validating it at write time turns a typo into a 422
# on the form the operator is looking at, instead of a 400 from OpenAI on somebody
# else's request an hour later — by which point the gateway looks broken and the config
# screen looks fine.
#
# The allowlist is deliberately the *documented* OpenAI generation parameters (SPEC
# §12.1) and nothing else. A provider-specific knob is not silently accepted here: it
# belongs in `extra_headers` or in a dialect adapter, both of which are visible.


@dataclass(frozen=True, slots=True)
class Bound:
    """What one parameter may be.

    ``kinds`` is checked before the range, so ``temperature: "warm"`` reports a type
    problem rather than a comparison failure.
    """

    kinds: tuple[type, ...]
    minimum: float | None = None
    maximum: float | None = None
    #: Rendered in the message. Worth spelling out — "must be a number" is less useful
    #: than "must be a number between 0 and 2".
    description: str = ""

    def check(self, name: str, value: Any) -> str | None:
        # `bool` is an `int` in Python, and `n: true` is a mistake worth catching.
        if isinstance(value, bool) or not isinstance(value, self.kinds):
            return f"'{name}' {self.description}"
        if isinstance(value, int | float):
            if self.minimum is not None and value < self.minimum:
                return f"'{name}' {self.description}"
            if self.maximum is not None and value > self.maximum:
                return f"'{name}' {self.description}"
        return None


#: Chosen to match the providers rather than to be generous: OpenAI rejects a
#: `temperature` above 2 and a `frequency_penalty` outside ±2, so accepting them here
#: would only move the failure to request time, which is the thing this prevents.
PARAMETER_BOUNDS: dict[str, Bound] = {
    "temperature": Bound((int, float), 0.0, 2.0, "must be a number between 0 and 2"),
    "top_p": Bound((int, float), 0.0, 1.0, "must be a number between 0 and 1"),
    "max_tokens": Bound((int,), 1, 1_000_000, "must be a whole number of at least 1"),
    "max_completion_tokens": Bound((int,), 1, 1_000_000, "must be a whole number of at least 1"),
    "presence_penalty": Bound((int, float), -2.0, 2.0, "must be a number between -2 and 2"),
    "frequency_penalty": Bound((int, float), -2.0, 2.0, "must be a number between -2 and 2"),
    "n": Bound((int,), 1, 128, "must be a whole number between 1 and 128"),
    "seed": Bound((int,), None, None, "must be a whole number"),
    "stop": Bound((str, list), None, None, "must be a string or a list of up to 4 strings"),
    "response_format": Bound((dict,), None, None, "must be an object"),
}

#: Longest a single `stop` sequence may be, and how many are allowed. Both are the
#: provider's limits; exceeding either is a 400 at request time.
MAX_STOP_SEQUENCES = 4
MAX_STOP_LENGTH = 64


def allowed_parameters() -> tuple[str, ...]:
    return tuple(sorted(PARAMETER_BOUNDS))


def validate_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the parameters unchanged, or raise :class:`Validation` naming the field.

    Unknown keys are refused rather than passed through. A silently forwarded
    ``temprature`` is a default that never applies and gives no sign of it — the failure
    mode this whole function exists to remove.
    """
    if not params:
        return {}

    for name, value in params.items():
        bound = PARAMETER_BOUNDS.get(name)
        if bound is None:
            raise Validation(
                f"'{name}' is not a generation parameter this gateway sets. "
                f"Allowed: {', '.join(allowed_parameters())}.",
                param=f"default_params.{name}",
            )
        if problem := bound.check(name, value):
            raise Validation(problem, param=f"default_params.{name}")
        if name == "stop":
            _check_stop(value)

    return dict(params)


def _check_stop(value: Any) -> None:
    sequences = [value] if isinstance(value, str) else value
    if len(sequences) > MAX_STOP_SEQUENCES:
        raise Validation(
            f"'stop' accepts at most {MAX_STOP_SEQUENCES} sequences.", param="default_params.stop"
        )
    for sequence in sequences:
        if not isinstance(sequence, str) or len(sequence) > MAX_STOP_LENGTH:
            raise Validation(
                f"Each 'stop' sequence must be a string of at most {MAX_STOP_LENGTH} characters.",
                param="default_params.stop",
            )
