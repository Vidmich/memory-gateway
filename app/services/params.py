"""Generation parameters: what an operator may set, and how the layers combine.

Two halves. :func:`validate_params` is the write-time gate on operator-authored
configuration; :func:`resolve_params` is the request-time merge. They live together
because the allowlist below is exactly the set of keys the merge is allowed to produce.

The merge order, lowest precedence first:

1. the upstream model's ``default_params``  — sensible values for that provider,
2. the gateway's ``param_overrides``        — the org's house style for this endpoint,
3. the client's request                     — the caller's explicit intent,
4. the gateway's ``locked_params``          — values the client may not change.

Layers 2 and 4 are the same idea at different strengths, and the difference is only where
they sit in this list. An *override* is a default the client can beat; a *lock* wins. Both
have to exist: pinning ``temperature`` for every caller and suggesting one are different
policies, and a system with only the first makes the endpoint useless for anyone with a
legitimate reason to differ.

A lock that actually changed something is reported back — see :class:`Resolved` — because
silently ignoring what a client asked for is the failure mode this design has to answer
for. The proxy turns that into a response header.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.errors import Validation


@dataclass(frozen=True, slots=True)
class Resolved:
    """The merged parameters, and which of the client's own values a lock replaced.

    ``overridden`` lists only keys the client actually sent with a *different* value.
    Locking ``temperature: 0.2`` and receiving ``temperature: 0.2`` overrode nothing, and
    reporting it would train people to ignore the header.
    """

    values: dict[str, Any]
    overridden: tuple[str, ...] = ()


def resolve_params(
    *,
    model_defaults: Mapping[str, Any] | None,
    gateway_overrides: Mapping[str, Any] | None,
    client: Mapping[str, Any] | None,
    locked: Mapping[str, Any] | None = None,
) -> Resolved:
    resolved: dict[str, Any] = {}
    resolved.update(model_defaults or {})
    resolved.update(gateway_overrides or {})
    resolved.update(client or {})

    sent = client or {}
    overridden = tuple(
        name for name, value in (locked or {}).items() if name in sent and sent[name] != value
    )
    # Last, so a lock is a cap rather than another default. Ignored values are reported
    # rather than merged: SPEC §12.1's principle is that a field the gateway will not
    # honour fails loudly, and this is the one case where refusing the request outright
    # would be worse — the client asked for something reasonable, the org said no.
    resolved.update(locked or {})
    return Resolved(values=resolved, overridden=overridden)


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


def validate_params(
    params: Mapping[str, Any] | None, *, field: str = "default_params"
) -> dict[str, Any]:
    """Return the parameters unchanged, or raise :class:`Validation` naming the field.

    Unknown keys are refused rather than passed through. A silently forwarded
    ``temprature`` is a default that never applies and gives no sign of it — the failure
    mode this whole function exists to remove.

    ``field`` is the form field the value came from — ``default_params`` on a model,
    ``param_overrides`` or ``locked_params`` on a gateway — so the 422 lands on the input
    the user is looking at rather than on whichever one this function was written for.
    """
    if not params:
        return {}

    for name, value in params.items():
        bound = PARAMETER_BOUNDS.get(name)
        if bound is None:
            raise Validation(
                f"'{name}' is not a generation parameter this gateway sets. "
                f"Allowed: {', '.join(allowed_parameters())}.",
                param=f"{field}.{name}",
            )
        if problem := bound.check(name, value):
            raise Validation(problem, param=f"{field}.{name}")
        if name == "stop":
            _check_stop(value, field=field)

    return dict(params)


def _check_stop(value: Any, *, field: str) -> None:
    sequences = [value] if isinstance(value, str) else value
    if len(sequences) > MAX_STOP_SEQUENCES:
        raise Validation(
            f"'stop' accepts at most {MAX_STOP_SEQUENCES} sequences.", param=f"{field}.stop"
        )
    for sequence in sequences:
        if not isinstance(sequence, str) or len(sequence) > MAX_STOP_LENGTH:
            raise Validation(
                f"Each 'stop' sequence must be a string of at most {MAX_STOP_LENGTH} characters.",
                param=f"{field}.stop",
            )
