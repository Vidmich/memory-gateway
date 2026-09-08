"""Versioned JSONB configuration blobs.

Three properties, shared by every settings blob in the product — a gateway's memory,
logging and limits sections, a connector's chunking settings, and the platform's own
configuration.

**Every field has a default, and unknown keys are ignored on load.** A row written before
a field existed loads with the default; a row written by a *newer* build that has since
been rolled back loads without its extra keys instead of crashing the read path. That is
the whole reason :meth:`ConfigBlob.load` is separate from ordinary validation.

**A write is strict.** :func:`merge_config` refuses a key the schema does not define, so
``{"doc_top_kk": 8}`` is a 422 on the form rather than a setting that silently never
applies — the same rule, and the same reasoning, as the parameter allowlist in
:mod:`app.services.params`.

**The defaults are the documentation.** A row stores ``{}`` and the API answers with the
full object, so what is shown is always what will actually happen.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict

from app.core.errors import Validation

#: Bumped when a field changes meaning, not when one is added. Stored on the row so a
#: future migration can tell "never written" from "written by version 1".
CONFIG_VERSION = 1


class ConfigBlob(BaseModel):
    """Base for the three blobs. Permissive on load, strict on write."""

    # Ignore rather than forbid: this class is what a *stored* row is parsed with, and a
    # row from a newer build must still load. `merge_config` does the strict check.
    model_config = ConfigDict(extra="ignore")

    version: int = CONFIG_VERSION

    @classmethod
    def load(cls, stored: Mapping[str, Any] | None) -> Self:
        """Parse a stored blob, filling in every default. ``{}`` is a valid input."""
        return cls.model_validate(dict(stored or {}))


def merge_config[BlobT: BaseModel](
    schema: type[BlobT],
    stored: Mapping[str, Any] | None,
    patch: Mapping[str, Any] | None,
    *,
    field: str,
) -> dict[str, Any]:
    """Deep-merge ``patch`` into ``stored`` and validate the result against ``schema``.

    A partial update, so ``{"doc_top_k": 8}`` changes one knob and leaves the rest alone.
    Unknown keys are refused rather than merged: a stored setting nothing reads is
    indistinguishable from a setting that does not work, and the second is what the user
    will conclude.

    ``field`` names the form field, so the 422 lands on the section the user is editing
    instead of in a banner.

    Bounded by :class:`~pydantic.BaseModel` rather than by :class:`ConfigBlob`, because
    task 17's platform sections are plain models — the merge only ever reads a schema's
    fields and validates against it, and requiring the versioned base would have meant
    giving six settings sections a ``version`` field to satisfy a type parameter.
    """
    merged = _deep_merge(dict(stored or {}), dict(patch or {}))
    _reject_unknown(schema, merged, field=field)
    try:
        blob = schema.model_validate(merged)
    except Exception as exc:
        location, message = _first_problem(exc)
        # Dotted: ``logging_config.redaction_patterns``. The section is what makes the
        # message unambiguous when two blobs happen to share a field name; the leaf is
        # what the form puts the message next to.
        raise Validation(message, param=f"{field}.{location}" if location else field) from exc
    return blob.model_dump(mode="json")


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursive for nested objects; a list is replaced wholesale.

    Today's blobs are flat, so this is one level in practice. It is written recursively
    because task 10's retrieval settings are the obvious place for a nested object, and a
    shallow merge that silently discarded its siblings would be found the hard way.
    """
    result = dict(base)
    for key, value in patch.items():
        current = result.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            result[key] = _deep_merge(current, value)
        else:
            # Lists are replaced: there is no sensible "merge" of two redaction pattern
            # lists, and appending would make removing one impossible.
            result[key] = value
    return result


def _reject_unknown(schema: type[BaseModel], merged: Mapping[str, Any], *, field: str) -> None:
    """Refuse a key the schema does not define, at any depth.

    Recursive because :func:`_deep_merge` is: task 14's ``limits.per_end_user`` is the
    first nested object in the product, and a check that stopped at the top level would
    let ``{"per_end_user": {"requests_per_minutes": 60}}`` through — accepted, stored,
    and silently never applied, which is the exact failure this function exists to
    prevent one level up.
    """
    known = schema.model_fields
    for key, value in merged.items():
        if key not in known:
            raise Validation(
                f"'{key}' is not a setting on this section. "
                f"Allowed: {', '.join(sorted(set(known) - {'version'}))}.",
                param=f"{field}.{key}",
            )
        nested = known[key].annotation
        if not isinstance(value, Mapping) or not isinstance(nested, type):
            continue
        if issubclass(nested, BaseModel):
            _reject_unknown(nested, value, field=f"{field}.{key}")


def _first_problem(exc: Exception) -> tuple[str, str]:
    """``(location, message)`` for the first failure, without Pydantic's envelope.

    The location is returned separately rather than folded into the text because it is
    what names the form input. A message reading "redaction_patterns: ..." next to the
    *redaction patterns* box says the same thing twice.
    """
    errors = getattr(exc, "errors", None)
    if callable(errors):
        for error in errors():
            location = ".".join(str(part) for part in error.get("loc", ()))
            message = str(error.get("msg", "")).removeprefix("Value error, ")
            return location, f"{location}: {message}" if location else message
    return "", str(exc)


__all__ = [
    "CONFIG_VERSION",
    "ConfigBlob",
    "merge_config",
]
