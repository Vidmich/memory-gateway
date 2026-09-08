"""Upstream dialect adapters.

Importing the concrete adapters here is what populates the registry in
``app.adapters.base``; a dialect nobody imports is a dialect ``get_adapter`` cannot find.
That import is also what makes a dialect *selectable* — :class:`app.services.catalog.
CatalogService` refuses one with no registered adapter — so this file is the single place
that says which dialects this build serves.
"""

from __future__ import annotations

from app.adapters import anthropic as _anthropic  # noqa: F401  registers "anthropic"
from app.adapters import openai as _openai  # noqa: F401  registers "openai"
from app.adapters.base import UpstreamAdapter, UpstreamTarget, get_adapter, known_dialects

__all__ = ["UpstreamAdapter", "UpstreamTarget", "get_adapter", "known_dialects"]
