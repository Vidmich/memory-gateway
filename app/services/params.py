"""Generation-parameter resolution.

Order, lowest precedence first:

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
from typing import Any


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
