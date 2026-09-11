"""The index fingerprint (task 104): everything a stored point depends on, defined once.

Task 20 recorded ``chunk_fingerprint`` — a digest of the effective chunking, with the
embedding model folded in for the strategies whose boundaries came out of it, and later the
tokenizer (task 101) and the contextual summarization identity (task 102). It answered "was
this document cut the way the connector is set now", and it answered it as one opaque
number: two rows that differ say *that* they differ and nothing about why.

This module replaces the opaque number with a **structured** one. An index fingerprint is
five segments, each a short digest of one input::

    ch=<chunking settings>;em=<embedding model>;tk=<tokenizer>;sm=<summarization>;xv=<extractor>

Two properties follow from the shape, and both are the reason for it. First,
:func:`stale_reason` is a comparison of segments, so a stale row can say *which* input
moved — chunking, the embedding model, the tokenizer, summarization, or the extractor —
in words a person can act on, without a second table remembering what the row was cut
with. Second, one input can be rewritten without recomputing the rest:
:func:`with_embedding_model` is how the platform reindex (task 17) marks a copied point as
embedded by the model it just re-embedded it with, which it can do because the copy changed
exactly one segment.

Everything a stored point depends on is in here and nothing else is. The extraction version
is the input that had no trigger before: a PDF extractor upgrade changes the text the chunks
were cut from, and until now nothing anywhere said the old chunks were of a different text.
The embedding model is in *every* fingerprint, not only under ``semantic``: the vector is
what is stored, and a vector from a different model is a different point whatever the
boundaries. Task 20's :func:`~app.schemas.connector_config.fingerprint` is the ``ch``
segment's input — settings only — and keeps its own meaning for the rows and points that
still carry it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.schemas.connector_config import ChunkingConfig, fingerprint
from app.schemas.summarization import ContextIdentity

#: The segments, in the order they are written. Also the order :func:`stale_reason`
#: reports them in when more than one moved: the embedding model first, because it is the
#: platform's change rather than the connector's and the one with the biggest bill behind
#: it; the extractor last, because it is the rarest.
SEGMENTS: tuple[str, ...] = ("em", "tk", "ch", "sm", "xv")

#: Reason codes, one per segment plus the one for a row that recorded nothing. Open by
#: design like ``documents.reason``: a screen that does not recognise one falls back to
#: the sentence.
CHUNKING = "chunking"
EMBEDDING_MODEL = "embedding_model"
TOKENIZER = "tokenizer"
SUMMARIZATION = "summarization"
EXTRACTOR = "extractor"
UNRECORDED = "unrecorded"

REASON_OF_SEGMENT: Mapping[str, str] = {
    "ch": CHUNKING,
    "em": EMBEDDING_MODEL,
    "tk": TOKENIZER,
    "sm": SUMMARIZATION,
    "xv": EXTRACTOR,
}

#: What the run history and the audit trail call each reason as a trigger. The same words
#: as the reasons, plus ``manual`` for a run somebody started with nothing stale.
TRIGGERS: tuple[str, ...] = (
    CHUNKING,
    EMBEDDING_MODEL,
    TOKENIZER,
    SUMMARIZATION,
    EXTRACTOR,
    "manual",
)

_NONE = "-"


def index_fingerprint(
    chunking: ChunkingConfig,
    *,
    embedding_model: str,
    tokenizer: str,
    context: ContextIdentity | None,
    extraction_version: int,
) -> str:
    """The fingerprint ingestion writes for a document cut under these inputs.

    ``chunking`` is the *effective* configuration for the document's format — the caller
    resolves the override, because only the caller knows the format. ``context`` is the
    contextual-summarization identity or ``None`` under ``off`` and ``summary_chunk``, the
    same rule as task 20's fingerprint: switching summary chunks on must not recut a corpus.
    """
    return ";".join(
        (
            f"ch={fingerprint(chunking)}",
            f"em={_digest(embedding_model)}",
            f"tk={_digest(tokenizer)}",
            f"sm={_digest(json.dumps(context.payload(), sort_keys=True)) if context else _NONE}",
            f"xv={int(extraction_version)}",
        )
    )


def parse(value: str | None) -> dict[str, str] | None:
    """The segments of a fingerprint, or ``None`` for a blank or for task 20's opaque
    digest — which is a value this module cannot read and therefore treats as unrecorded
    rather than as wrong."""
    if not value:
        return None
    parts: dict[str, str] = {}
    for piece in value.split(";"):
        key, _, rest = piece.partition("=")
        if not rest or key not in REASON_OF_SEGMENT:
            return None
        parts[key] = rest
    return parts if set(parts) == set(SEGMENTS) else None


def embedding_segment(embedding_model: str) -> str:
    """The ``em=`` segment for a model, for a store rewriting it in place."""
    return f"em={_digest(embedding_model)}"


def with_embedding_model(value: str, embedding_model: str) -> str:
    """The same fingerprint under a different embedding model.

    For a point the platform reindex re-embedded from its stored text: the chunking, the
    tokenizer, the summarization and the extractor are exactly what they were, and the
    vector is now the new model's. A value this module cannot parse comes back unchanged —
    an old digest stays an old digest, and the row stays *unrecorded* rather than being
    given a fingerprint it never had.
    """
    if parse(value) is None:
        return value
    return ";".join(
        f"em={_digest(embedding_model)}" if piece.startswith("em=") else piece
        for piece in value.split(";")
    )


@dataclass(frozen=True, slots=True)
class Reason:
    """Why a row is stale, as a code the screen can branch on and a sentence it can show."""

    code: str
    sentence: str

    @property
    def is_unrecorded(self) -> bool:
        return self.code == UNRECORDED


def stale_reason(recorded: str | None, expected: str) -> str | None:
    """Which input moved between what a row recorded and what ingestion would write now.

    ``None`` when they agree. :data:`UNRECORDED` when the row has no readable fingerprint
    — a document indexed before this was recorded, or under task 20's digest — which is
    shown differently and never counted as stale, because it is not known to be wrong.
    Otherwise the first differing segment in :data:`SEGMENTS` order; the caller that wants
    all of them has :func:`differences`.
    """
    changed = differences(recorded, expected)
    if changed is None:
        return UNRECORDED
    return changed[0] if changed else None


def differences(recorded: str | None, expected: str) -> list[str] | None:
    """Every reason the two disagree, in report order; ``None`` for an unreadable row."""
    was = parse(recorded)
    now = parse(expected)
    if was is None or now is None:
        return None
    return [REASON_OF_SEGMENT[segment] for segment in SEGMENTS if was[segment] != now[segment]]


def reason_sentence(
    code: str,
    *,
    was: Mapping[str, Any] | None = None,
    now: Mapping[str, Any] | None = None,
) -> str:
    """The reason as a sentence, naming the old and new values where the row kept them in
    clear (``embedding_model``, ``tokenizer``, ``chunk_strategy``) — so *"embedded with
    text-embedding-3-small; the platform is now text-embedding-3-large"* rather than
    *"embedding model changed"*."""
    was = was or {}
    now = now or {}
    if code == EMBEDDING_MODEL:
        return _pair(
            "Embedded with",
            was.get("embedding_model"),
            "the platform is now",
            now.get("embedding_model"),
        )
    if code == TOKENIZER:
        return _pair(
            "Sized with", was.get("tokenizer"), "the tokenizer is now", now.get("tokenizer")
        )
    if code == CHUNKING:
        old, new = was.get("chunk_strategy"), now.get("chunk_strategy")
        if old and new and old != new:
            return f"Cut with '{old}'; the connector now chunks with '{new}'."
        return "The chunking settings changed since this document was cut."
    if code == SUMMARIZATION:
        return "The contextual summarization its vectors were built with changed."
    if code == EXTRACTOR:
        return (
            "The extractor for this format was upgraded; the text the chunks were cut from "
            "may differ."
        )
    if code == UNRECORDED:
        return (
            "Indexed before the fingerprint was recorded. Not known to be stale — reprocess "
            "to record one."
        )
    return "Indexed under a previous configuration."


def _pair(before: str, old: Any, after: str, new: Any) -> str:
    if old and new:
        return f"{before} {old}; {after} {new}."
    return f"{before} a previous setting; {after} different."


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "CHUNKING",
    "EMBEDDING_MODEL",
    "EXTRACTOR",
    "REASON_OF_SEGMENT",
    "SEGMENTS",
    "SUMMARIZATION",
    "TOKENIZER",
    "TRIGGERS",
    "UNRECORDED",
    "Reason",
    "differences",
    "embedding_segment",
    "index_fingerprint",
    "parse",
    "reason_sentence",
    "stale_reason",
    "with_embedding_model",
]
