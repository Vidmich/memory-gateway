"""Upstream dialect adapters.

Importing the concrete adapters here is what populates the registry in
``app.adapters.base``; a dialect nobody imports is a dialect ``get_adapter`` cannot find.
Task 16 adds ``anthropic`` alongside it.
"""

from __future__ import annotations

from app.adapters import openai as _openai  # noqa: F401  registers the "openai" dialect
from app.adapters.base import UpstreamAdapter, UpstreamTarget, get_adapter, known_dialects

__all__ = ["UpstreamAdapter", "UpstreamTarget", "get_adapter", "known_dialects"]
