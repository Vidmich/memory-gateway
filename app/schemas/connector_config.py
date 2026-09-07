"""A connector's chunking settings (SPEC §9.3).

Chunking is the one knob in this product where the wrong value is invisible: retrieval
still returns *something*, it is just worse. So the constraints here are not decoration.

``overlap`` must be smaller than ``chunk_size``, and by a real margin. Overlap greater
than or equal to the size is not a slow configuration, it is a non-terminating one — each
chunk would start at or before the previous chunk's start and the splitter would never
advance. Capping it at half is the point where the index stops being mostly duplicates.

Changing any of these invalidates every chunk already stored, which is why
:class:`~app.services.connectors.ConnectorService` reports whether a patch requires a
reindex rather than quietly leaving an index built under the old settings in place.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from app.schemas.config import ConfigBlob

#: SPEC §9.3. ``recursive`` splits on paragraph → sentence → token boundaries;
#: ``by_heading`` splits on document structure and falls back to ``recursive`` for
#: formats that have none; ``fixed`` is the escape hatch — exact token windows, no
#: boundary awareness — for content whose structure is meaningless (minified data,
#: single-line logs) where boundary hunting only produces uneven chunks.
CHUNK_STRATEGIES = ("recursive", "fixed", "by_heading")

#: The ceiling is not a guess: an embedding model's context is typically 8192 tokens, and
#: a chunk that cannot be embedded is a document that can never leave ``embedding``.
MAX_CHUNK_SIZE = 4000
MIN_CHUNK_SIZE = 50


class ChunkingConfig(ConfigBlob):
    """SPEC §9.3, with the defaults it names."""

    strategy: Literal["recursive", "fixed", "by_heading"] = "recursive"
    chunk_size: int = Field(default=1000, ge=MIN_CHUNK_SIZE, le=MAX_CHUNK_SIZE)
    overlap: int = Field(default=150, ge=0, le=MAX_CHUNK_SIZE // 2)
    #: Do not split mid-sentence or mid-code-block. Off means "cut at exactly
    #: ``chunk_size`` tokens", which is what ``fixed`` does anyway.
    respect_boundaries: bool = True

    @model_validator(mode="after")
    def _overlap_leaves_room(self) -> Self:
        if self.overlap * 2 > self.chunk_size:
            raise ValueError(
                "overlap must be at most half of chunk_size, or nearly every chunk is a "
                f"copy of its neighbour (chunk_size={self.chunk_size}, overlap={self.overlap})"
            )
        return self


#: Fields whose change makes every stored chunk wrong. ``version`` is not one of them.
REINDEX_TRIGGERS = ("strategy", "chunk_size", "overlap", "respect_boundaries")


def requires_reindex(before: ChunkingConfig, after: ChunkingConfig) -> bool:
    """Whether moving from one configuration to the other invalidates the existing index.

    Every field currently triggers it, which makes this function look redundant. It is
    not: it is the *statement* that the set is enumerated rather than "any change at
    all", so task 11 adding a per-format option that does not affect chunk boundaries is
    a one-line decision here instead of a silent full reindex of every connector.
    """
    return any(getattr(before, name) != getattr(after, name) for name in REINDEX_TRIGGERS)


__all__ = [
    "CHUNK_STRATEGIES",
    "MAX_CHUNK_SIZE",
    "MIN_CHUNK_SIZE",
    "REINDEX_TRIGGERS",
    "ChunkingConfig",
    "requires_reindex",
]
