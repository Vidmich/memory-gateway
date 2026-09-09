"""A connector's chunking settings (SPEC §9.3).

Chunking is the one knob in this product where the wrong value is invisible: retrieval
still returns *something*, it is just worse. So the constraints here are not decoration.

``overlap`` must be smaller than ``chunk_size``, and by a real margin. Overlap greater
than or equal to the size is not a slow configuration, it is a non-terminating one — each
chunk would start at or before the previous chunk's start and the splitter would never
advance. Capping it at half is the point where the index stops being mostly duplicates.

**A connector is a source, not a format.** A repository holds code and Markdown; a shared
drive holds PDFs and spreadsheets. One strategy for all of them is the wrong answer by
construction, so :attr:`ChunkingConfig.overrides` maps a *format kind* — the closed set
:data:`~app.services.filetypes.FORMAT_KINDS`, the same classification the extraction
metrics are labelled by — to a partial configuration. The configuration a document is
actually cut with is :func:`effective`, and it is what the connector screen displays: a
resolution rule nobody can see is a rule everybody guesses at.

Changing any of these invalidates the chunks already stored, which is why
:class:`~app.services.connectors.ConnectorService` reports whether a patch requires a
reindex. Since overrides exist, that answer is a *set of formats* rather than a boolean —
adding an override for code should reindex the code files and leave the PDFs alone.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.config import ConfigBlob
from app.services.filetypes import FORMAT_KINDS

#: SPEC §9.3, as amended by task 20. The first three cut on a token budget adjusted for
#: where punctuation happens to be; the last three cut on something else entirely.
#:
#: ``recursive``       a token window walked back to the nearest paragraph, sentence, then
#:                     word boundary.
#: ``fixed``           exact token windows, no boundary hunting — the escape hatch for
#:                     content whose structure is meaningless.
#: ``by_heading``      the document's own sections.
#: ``semantic``        cut where consecutive sentences stop being about the same thing,
#:                     measured against the document's own distribution of distances.
#: ``sentence_window`` embed a sentence, return it with its neighbours: a small unit to
#:                     match on and enough context to answer with.
#: ``code``            function and class bodies as units, for the languages named in
#:                     :data:`~app.services.filetypes.CODE_LANGUAGES`.
CHUNK_STRATEGIES = ("recursive", "fixed", "by_heading", "semantic", "sentence_window", "code")

Strategy = Literal["recursive", "fixed", "by_heading", "semantic", "sentence_window", "code"]

#: Strategies whose *boundaries* come from the embedding model, not just their vectors.
#: This is the set that makes a platform embedding-model change expensive: re-embedding
#: the stored chunk text of a semantically-cut document faithfully reproduces the old
#: model's opinion about where the topics changed.
MODEL_DEPENDENT_STRATEGIES = frozenset({"semantic"})

#: The ceiling is not a guess: an embedding model's context is typically 8192 tokens, and
#: a chunk that cannot be embedded is a document that can never leave ``embedding``.
MAX_CHUNK_SIZE = 4000
MIN_CHUNK_SIZE = 50

#: Fields whose change makes every stored chunk wrong. ``version`` is not one of them,
#: and neither is ``overrides`` — a change there is answered per format by
#: :func:`changed_formats`, which compares *effective* configurations.
REINDEX_TRIGGERS = (
    "strategy",
    "chunk_size",
    "overlap",
    "respect_boundaries",
    "breakpoint_percentile",
    "min_chunk_size",
    "window_sentences",
)


class ChunkingOverride(BaseModel):
    """A partial :class:`ChunkingConfig`, for one format kind.

    Every field optional, with ``None`` meaning "inherit" — spelled out rather than
    derived from :class:`ChunkingConfig` by a metaclass. These fields are the API: they
    appear in the OpenAPI document and in the generated client, and a model generated
    from another one with subtly different validation is a bug that only shows up in the
    wrong half of a form.

    Not a :class:`~app.schemas.config.ConfigBlob`, deliberately. A blob carries a
    ``version`` because it is a stored document; an override is a *fragment* of one, and
    giving each entry its own version would put a required field with no meaning into
    every request that sets one. It keeps the blob's permissive-on-load behaviour, which
    is the half that matters here.
    """

    model_config = ConfigDict(extra="ignore")

    strategy: Strategy | None = None
    chunk_size: int | None = Field(default=None, ge=MIN_CHUNK_SIZE, le=MAX_CHUNK_SIZE)
    overlap: int | None = Field(default=None, ge=0, le=MAX_CHUNK_SIZE // 2)
    respect_boundaries: bool | None = None
    breakpoint_percentile: int | None = Field(default=None, ge=50, le=99)
    min_chunk_size: int | None = Field(default=None, ge=0, le=MAX_CHUNK_SIZE)
    window_sentences: int | None = Field(default=None, ge=0, le=10)

    def changes(self) -> dict[str, Any]:
        """The fields this override actually sets. ``version`` is never one of them."""
        return {
            name: value for name in REINDEX_TRIGGERS if (value := getattr(self, name)) is not None
        }


class ChunkingConfig(ConfigBlob):
    """SPEC §9.3, with the defaults it names."""

    strategy: Strategy = "recursive"
    chunk_size: int = Field(default=1000, ge=MIN_CHUNK_SIZE, le=MAX_CHUNK_SIZE)
    overlap: int = Field(default=150, ge=0, le=MAX_CHUNK_SIZE // 2)
    #: Do not split mid-sentence or mid-code-block. Off means "cut at exactly
    #: ``chunk_size`` tokens", which is what ``fixed`` does anyway.
    respect_boundaries: bool = True

    #: ``semantic`` only. Which percentile of *this document's* consecutive-sentence
    #: distances counts as a topic change. A percentile rather than an absolute threshold
    #: because the distance scale is a property of the embedding model: an absolute number
    #: would need retuning every time the platform model changed, and nobody would.
    breakpoint_percentile: int = Field(default=85, ge=50, le=99)
    #: ``semantic`` only. The floor that stops a document of short declarative sentences
    #: becoming one chunk per sentence — which is ``sentence_window`` without the window,
    #: and worse than either. Capped at half the ceiling by :func:`size_floor`.
    min_chunk_size: int = Field(default=200, ge=0, le=MAX_CHUNK_SIZE)
    #: ``sentence_window`` only. Neighbours kept on each side of the embedded sentence.
    window_sentences: int = Field(default=2, ge=0, le=10)

    #: Per-format overrides, keyed by :data:`~app.services.filetypes.FORMAT_KINDS`.
    overrides: dict[str, ChunkingOverride] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _overlap_leaves_room(self) -> Self:
        if self.overlap * 2 > self.chunk_size:
            raise ValueError(
                "overlap must be at most half of chunk_size, or nearly every chunk is a "
                f"copy of its neighbour (chunk_size={self.chunk_size}, overlap={self.overlap})"
            )
        return self

    @model_validator(mode="after")
    def _overrides_name_real_formats(self) -> Self:
        unknown = sorted(set(self.overrides) - set(FORMAT_KINDS))
        if unknown:
            # Refused rather than ignored. An override under a misspelled key is a setting
            # that is stored, displayed, and applied to nothing — which is the failure the
            # strict-on-write rule in `app.schemas.config` exists to prevent one level up.
            raise ValueError(
                f"{', '.join(unknown)}: not a format this build classifies. "
                f"Available: {', '.join(FORMAT_KINDS)}."
            )
        return self


def effective(config: ChunkingConfig, kind: str) -> ChunkingConfig:
    """The configuration a document of format ``kind`` is actually cut with.

    The connector's, with its override for that kind applied. The result carries **no**
    overrides of its own: an effective configuration is a leaf, and one that could itself
    be overridden is a resolution order somebody has to hold in their head.

    Re-validated rather than copied field by field, so an override that sets ``overlap``
    against an inherited ``chunk_size`` is checked against the pair that will actually be
    used. The combination is what has to be legal, and neither half of it is.
    """
    override = config.overrides.get(kind)
    base = config.model_dump(mode="json")
    base.pop("overrides", None)
    if override is not None:
        base.update(override.changes())
    return ChunkingConfig.model_validate(base)


def size_floor(config: ChunkingConfig) -> int:
    """``min_chunk_size``, capped at half the ceiling.

    Capped here rather than refused by a validator, and which path each would break is the
    reason. A floor above the ceiling is not a configuration anyone means, but validating
    it would make an already-stored row with a small ``chunk_size`` fail to *load* the
    moment this field gained its default — a read-path failure for a write-path mistake
    nobody made.
    """
    return min(config.min_chunk_size, config.chunk_size // 2)


def depends_on_embedding_model(config: ChunkingConfig) -> bool:
    """Whether any format under this connector is cut *using* the embedding model.

    True means a platform model change has to recut this connector rather than re-embed
    it, which is strictly more expensive — see :mod:`app.services.reindex`.
    """
    return any(
        effective(config, kind).strategy in MODEL_DEPENDENT_STRATEGIES for kind in _kinds_in(config)
    )


def fingerprint(config: ChunkingConfig, *, embedding_model: str | None = None) -> str:
    """A short stable digest of one *effective* configuration.

    Recorded in every chunk's payload and on the document row, for exactly the reason SPEC
    §9.4 records the embedding model: a connector reindexed halfway holds two chunkings in
    one collection, and without this nothing says which chunk is which.

    The embedding model is folded in only for the strategies whose boundaries depend on
    it. Including it unconditionally would make every model change look like a chunking
    change for connectors whose chunks are in fact still correct.
    """
    payload: dict[str, Any] = {name: getattr(config, name) for name in REINDEX_TRIGGERS}
    if config.strategy in MODEL_DEPENDENT_STRATEGIES and embedding_model:
        payload["embedding_model"] = embedding_model
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def changed_formats(
    before: ChunkingConfig, after: ChunkingConfig, *, model_changed: bool = False
) -> frozenset[str]:
    """Which format kinds' stored chunks are invalidated by moving between the two.

    This is what the enumerated-triggers design was for. The old docstring predicted a
    per-format option; here it is, and the payoff is that adding an override for code
    reindexes the code files and leaves the PDFs alone.

    ``model_changed`` is not a formality either: under ``semantic`` the embedding model is
    part of the chunking configuration, because the boundaries came out of it.
    """
    changed: set[str] = set()
    for kind in _kinds_in(before) | _kinds_in(after):
        one = effective(before, kind)
        other = effective(after, kind)
        settings_differ = any(
            getattr(one, name) != getattr(other, name) for name in REINDEX_TRIGGERS
        )
        # The second clause is the one that is easy to miss: nothing about the connector
        # changed, and the chunks are stale anyway, because the model that decided where
        # to cut is not the model that will answer.
        model_matters = model_changed and other.strategy in MODEL_DEPENDENT_STRATEGIES
        if settings_differ or model_matters:
            changed.add(kind)
    return frozenset(changed)


def requires_reindex(
    before: ChunkingConfig, after: ChunkingConfig, *, model_changed: bool = False
) -> bool:
    """Whether moving from one configuration to the other invalidates any stored chunk."""
    return bool(changed_formats(before, after, model_changed=model_changed))


def _kinds_in(config: ChunkingConfig) -> set[str]:
    """Every format kind worth comparing.

    All of them, always — not only the overridden ones. A format that *loses* its override
    has to come back as changed, and it can only do that if it was compared at all.
    """
    return set(FORMAT_KINDS) | set(config.overrides)


__all__ = [
    "CHUNK_STRATEGIES",
    "MAX_CHUNK_SIZE",
    "MIN_CHUNK_SIZE",
    "MODEL_DEPENDENT_STRATEGIES",
    "REINDEX_TRIGGERS",
    "ChunkingConfig",
    "ChunkingOverride",
    "Strategy",
    "changed_formats",
    "depends_on_embedding_model",
    "effective",
    "fingerprint",
    "requires_reindex",
    "size_floor",
]
