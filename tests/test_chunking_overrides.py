"""Per-format overrides: resolution, fingerprints, and which formats a change invalidates.

A connector is a *source*, not a format. A repository holds code and Markdown; a shared
drive holds PDFs and spreadsheets. One strategy for all of them is the wrong answer by
construction — so the tests here are mostly about the two functions that make the
difference visible rather than inferred: :func:`~app.schemas.connector_config.effective`,
which says what a document is actually cut with, and
:func:`~app.schemas.connector_config.changed_formats`, which says whose chunks a change
just invalidated.

The second one is the payoff of a decision made long before this task. ``requires_reindex``
was written as an enumerated set of triggers rather than "did any field change", and its
docstring said the enumeration would earn its keep when a per-format option arrived. This
is that, and the difference it buys is a connector where adding an override for code
re-runs the code files and leaves a thousand PDFs indexed.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.errors import Validation
from app.schemas.config import merge_config
from app.schemas.connector_config import (
    ChunkingConfig,
    changed_formats,
    depends_on_embedding_model,
    effective,
    fingerprint,
    requires_reindex,
    size_floor,
)
from app.services.filetypes import FORMAT_KINDS, format_label


def config(**settings: object) -> ChunkingConfig:
    return ChunkingConfig.model_validate(settings)


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def test_a_document_with_no_override_is_cut_with_the_connector_settings() -> None:
    stored = config(strategy="recursive", chunk_size=800)

    assert effective(stored, "pdf").chunk_size == 800
    assert effective(stored, "pdf").strategy == "recursive"


def test_an_override_replaces_only_the_fields_it_sets() -> None:
    stored = config(
        strategy="recursive", chunk_size=800, overlap=100, overrides={"code": {"strategy": "code"}}
    )

    resolved = effective(stored, "code")

    assert resolved.strategy == "code"
    assert resolved.chunk_size == 800, "an unset field inherits"
    assert resolved.overlap == 100


def test_an_effective_configuration_carries_no_overrides_of_its_own() -> None:
    """A leaf. One that could itself be overridden is a resolution order somebody has to
    hold in their head, and there is no version of that which stays right."""
    stored = config(overrides={"pdf": {"chunk_size": 500}})

    assert effective(stored, "pdf").overrides == {}


def test_an_override_is_validated_against_the_fields_it_inherits() -> None:
    """An override that sets ``chunk_size`` alone can still make the *pair* illegal. The
    combination is what has to be legal, and neither half of it is."""
    stored = config(chunk_size=1000, overlap=400, overrides={"pdf": {"chunk_size": 100}})

    with pytest.raises(ValidationError, match="at most half"):
        effective(stored, "pdf")


def test_an_override_for_a_format_this_build_does_not_classify_is_refused() -> None:
    """Refused rather than ignored: an override under a misspelled key is a setting that is
    stored, displayed, and applied to nothing."""
    with pytest.raises(ValidationError, match="not a format"):
        config(overrides={"pdfs": {"chunk_size": 500}})


def test_an_unknown_setting_inside_an_override_is_refused_on_write() -> None:
    """The strict-on-write rule, one level deeper than it used to reach. A key nothing
    reads is indistinguishable from a setting that does not work, and the second is what
    the user will conclude."""
    with pytest.raises(Validation, match="chunk_sizes"):
        merge_config(
            ChunkingConfig, {}, {"overrides": {"pdf": {"chunk_sizes": 500}}}, field="chunking"
        )


def test_every_format_a_document_can_be_is_a_key_an_override_can_use() -> None:
    """One classification, not two. Two lists is how a file is classified as code by one
    part of the pipeline and as text by another."""
    seen = {
        format_label(media)
        for media in ("application/pdf", "text/markdown", "text/x-python", "video/mp4", "text/csv")
    }

    assert seen <= set(FORMAT_KINDS)


def test_the_floor_is_capped_rather_than_refused() -> None:
    """Validating it would make an already-stored row with a small ``chunk_size`` fail to
    *load* — a read-path failure for a write-path mistake nobody made."""
    assert size_floor(config(chunk_size=100, min_chunk_size=200, overlap=0)) == 50
    assert size_floor(config(chunk_size=1000, min_chunk_size=200)) == 200


# ---------------------------------------------------------------------------
# what a change invalidates
# ---------------------------------------------------------------------------


def test_a_connector_wide_change_invalidates_every_format() -> None:
    before = config(chunk_size=1000)
    after = config(chunk_size=500)

    assert changed_formats(before, after) == frozenset(FORMAT_KINDS)
    assert requires_reindex(before, after)


def test_adding_an_override_invalidates_only_that_format() -> None:
    """The whole point. The PDFs stay indexed."""
    before = config(chunk_size=1000)
    after = config(chunk_size=1000, overrides={"code": {"strategy": "code"}})

    assert changed_formats(before, after) == frozenset({"code"})


def test_removing_an_override_invalidates_that_format_too() -> None:
    """A format that *loses* its override has to come back as changed, which it can only do
    if every format is compared rather than only the overridden ones."""
    before = config(chunk_size=1000, overrides={"pdf": {"chunk_size": 400}})
    after = config(chunk_size=1000)

    assert changed_formats(before, after) == frozenset({"pdf"})


def test_an_override_that_changes_nothing_effective_invalidates_nothing() -> None:
    before = config(chunk_size=1000)
    after = config(chunk_size=1000, overrides={"pdf": {"chunk_size": 1000}})

    assert changed_formats(before, after) == frozenset()
    assert not requires_reindex(before, after)


def test_a_rename_or_a_description_change_is_not_a_chunking_change() -> None:
    """``version`` is not a trigger and neither is anything outside the enumerated set."""
    assert not requires_reindex(config(), config(version=2))


# ---------------------------------------------------------------------------
# the embedding model as part of the chunking configuration
# ---------------------------------------------------------------------------


def test_only_the_model_dependent_strategies_depend_on_the_model() -> None:
    assert depends_on_embedding_model(config(strategy="semantic"))
    assert depends_on_embedding_model(config(overrides={"markdown": {"strategy": "semantic"}}))
    assert not depends_on_embedding_model(config(strategy="recursive"))
    assert not depends_on_embedding_model(config(strategy="sentence_window"))


def test_a_model_change_invalidates_a_semantic_format_and_nothing_else() -> None:
    """Under ``semantic`` the boundaries came out of the model, so the chunks are stale even
    though not one connector setting moved. Every other format's chunks are still correct
    and only need re-embedding, which is a different and much cheaper operation."""
    stored = config(overrides={"markdown": {"strategy": "semantic"}})

    assert changed_formats(stored, stored, model_changed=True) == frozenset({"markdown"})
    assert not requires_reindex(stored, stored, model_changed=False)


def test_the_fingerprint_folds_in_the_model_only_where_it_matters() -> None:
    """Including it unconditionally would make every model change look like a chunking
    change for connectors whose chunks are in fact still correct."""
    recursive = config(strategy="recursive")
    semantic = config(strategy="semantic")

    assert fingerprint(recursive, embedding_model="a") == fingerprint(
        recursive, embedding_model="b"
    )
    assert fingerprint(semantic, embedding_model="a") != fingerprint(semantic, embedding_model="b")


def test_the_fingerprint_changes_with_every_reindex_trigger() -> None:
    """It is what tells two chunkings apart inside one half-reindexed collection, so a
    field that moved the boundaries and left the digest alone would be a silent gap."""
    base = config(strategy="recursive", chunk_size=1000, overlap=100)
    digests = {
        fingerprint(base),
        fingerprint(config(strategy="fixed", chunk_size=1000, overlap=100)),
        fingerprint(config(strategy="recursive", chunk_size=900, overlap=100)),
        fingerprint(config(strategy="recursive", chunk_size=1000, overlap=50)),
        fingerprint(config(strategy="recursive", chunk_size=1000, overlap=100, window_sentences=4)),
        fingerprint(
            config(strategy="recursive", chunk_size=1000, overlap=100, respect_boundaries=False)
        ),
    }

    assert len(digests) == 6


def test_the_fingerprint_is_short_and_stable() -> None:
    """Stored in every chunk payload, so its size is multiplied by the corpus."""
    digest = fingerprint(config())

    assert len(digest) == 16
    assert digest == fingerprint(config())
