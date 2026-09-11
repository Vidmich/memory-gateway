"""The tokenizer registry (task 101): derivation, the approximate tokenizer, calibration.

Everything here is pure. The one thing that touches the network — a tiktoken vocabulary
load — is exercised only through ``name`` on encodings that are already cached, and
``tests/test_tokenizer.py`` covers the degraded path.
"""

from __future__ import annotations

import json
import re
from itertools import pairwise
from pathlib import Path

import pytest

from app.schemas.connector_config import ChunkingConfig, changed_formats, fingerprint
from app.services.chunking import chunk_document
from app.services.extraction import Extracted, Section
from app.services.tokenizer import ApproximateTokenizer, count
from app.services.tokenizers import (
    DERIVATIONS,
    FALLBACK,
    TOKENIZER_NAMES,
    Calibration,
    DriftWindow,
    TokenizerSpec,
    calibrated,
    derive,
    effective,
    resolve,
    stored,
)
from tests.chunking_contract import check_invariants
from tests.test_chunking_strategies import PATHOLOGICAL, config, flat_signal

# ---------------------------------------------------------------------------
# the stored form
# ---------------------------------------------------------------------------


def test_approximate_requires_a_ratio_and_nothing_else_takes_one() -> None:
    with pytest.raises(ValueError, match="needs 'ratio'"):
        TokenizerSpec(name="approximate")
    with pytest.raises(ValueError, match="takes no ratio"):
        TokenizerSpec(name="o200k_base", ratio=4.0)
    assert TokenizerSpec(name="approximate", ratio=3.5).key == "approximate:3.5"
    assert TokenizerSpec(name="cl100k_base").key == "cl100k_base"


def test_a_misspelled_name_is_refused_rather_than_becoming_words() -> None:
    """The registry is closed: the failure is a 422 on the form, never a silent fallback."""
    with pytest.raises(ValueError):
        TokenizerSpec.model_validate({"name": "cl100k"})


def test_the_ratio_is_bounded() -> None:
    with pytest.raises(ValueError):
        TokenizerSpec.approximate(0.5)
    with pytest.raises(ValueError):
        TokenizerSpec.approximate(40)


def test_the_key_round_trips() -> None:
    for spec in (TokenizerSpec.approximate(3.6), TokenizerSpec(name="p50k_base")):
        assert TokenizerSpec.parse(spec.key) == spec


def test_a_stored_override_that_no_longer_validates_reads_as_none() -> None:
    """Permissive on the way out of the row, like every configuration blob."""
    assert stored(None) is None
    assert stored({"name": "no-such-thing"}) is None
    assert stored({"name": "approximate", "ratio": 3.2}) == TokenizerSpec.approximate(3.2)


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


def test_every_name_in_the_registry_resolves_to_a_tokenizer() -> None:
    for name in TOKENIZER_NAMES:
        spec = (
            TokenizerSpec.approximate(4.0)
            if name == "approximate"
            else TokenizerSpec.model_validate({"name": name})
        )
        tokenizer = resolve(spec)
        assert tokenizer.offsets("") == [0]
        assert tokenizer.offsets("two words")[-1] == len("two words")


def test_resolution_is_cached_per_specification() -> None:
    """Loading a vocabulary is what the worker runtime says is expensive."""
    assert resolve("approximate:3.5") is resolve(TokenizerSpec.approximate(3.5))
    assert resolve("approximate:3.5") is not resolve("approximate:3.6")


# ---------------------------------------------------------------------------
# derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dialect", "model_id", "expected"),
    [
        ("openai", "gpt-4o", "o200k_base"),
        ("openai", "gpt-4o-mini", "o200k_base"),
        ("openai", "o1-preview", "o200k_base"),
        ("openai", "o3-mini", "o200k_base"),
        ("openai", "gpt-4.1-nano", "o200k_base"),
        ("openai", "gpt-4-turbo", "cl100k_base"),
        ("openai", "gpt-3.5-turbo", "cl100k_base"),
        ("openai", "text-embedding-3-small", "cl100k_base"),
        ("openai", "text-embedding-ada-002", "cl100k_base"),
        ("openai", "text-davinci-003", "p50k_base"),
        ("anthropic", "claude-sonnet-4-5", "approximate:3.5"),
        ("anthropic", "anything-served-by-a-claude-endpoint", "approximate:3.5"),
        # OpenRouter and Together namespace by vendor; the tail of the id is what counts.
        ("openai", "openai/gpt-4o-mini", "o200k_base"),
        ("openai", "anthropic/claude-3.5-sonnet", "approximate:3.5"),
        ("openai", "meta-llama/Llama-3.3-70B-Instruct-Turbo", "approximate:4"),
        ("openai", "llama3.2", "approximate:4"),
        ("openai", "", "approximate:4"),
        ("hash", "hash-bow", "approximate:4"),
        ("some-future-dialect", "gpt-4o", "approximate:4"),
    ],
)
def test_derivation_by_prefix(dialect: str, model_id: str, expected: str) -> None:
    assert derive(dialect, model_id).key == expected


def test_the_longest_prefix_wins() -> None:
    """``gpt-4o`` is under ``gpt-4`` as a string and under a different encoding as a
    model; the table is only right if the longer row beats the shorter one."""
    assert derive("openai", "gpt-4o").key != derive("openai", "gpt-4").key


def test_every_derivation_row_names_a_registry_entry() -> None:
    for row in DERIVATIONS:
        assert row.spec.name in TOKENIZER_NAMES
    assert FALLBACK.name == "approximate"


def test_effective_says_where_it_came_from() -> None:
    derived = effective("openai", "gpt-4o", None)
    assert (derived.key, derived.origin, derived.overridden) == ("o200k_base", "derived", False)
    assert derived.label() == "o200k_base (derived)"

    overridden = effective("openai", "gpt-4o", TokenizerSpec.approximate(3.9))
    assert (overridden.key, overridden.origin) == ("approximate:3.9", "override")
    assert overridden.approximate and not derived.approximate


def test_parity_with_the_web_provider_presets() -> None:
    """Every model the UI offers as a preset derives something better than the fallback,
    or is one of the families we knowingly approximate. The web table and this one are
    different knowledge about the same providers, and this is where they are compared."""
    source = Path(__file__).resolve().parents[1] / "web" / "src" / "pages" / "providerPresets.ts"
    text = source.read_text(encoding="utf-8")
    presets = re.findall(r"modelId: '([^']+)'(?:,\s*dialect: '([^']+)')?", text)
    assert presets, "no presets parsed — did providerPresets.ts change shape?"

    approximated_families = ("llama", "mistral", "qwen")
    for model_id, dialect in presets:
        spec = derive(dialect or "openai", model_id)
        if any(family in model_id.lower() for family in approximated_families):
            assert spec.name == "approximate", (model_id, spec)
        else:
            assert spec != FALLBACK, f"{model_id!r} fell through the derivation table"


# ---------------------------------------------------------------------------
# the approximate tokenizer
# ---------------------------------------------------------------------------

PROSE = (
    "The quick brown fox jumps over the lazy dog. Pack my box with five dozen liquor jugs. " * 12
).strip()


@pytest.mark.parametrize("ratio", [1.0, 2.5, 3.5, 3.6, 4.0, 6.0, 20.0])
def test_the_approximate_tokenizer_honours_the_port(ratio: float) -> None:
    tokenizer = ApproximateTokenizer(ratio)
    for text in (PROSE, "x" * 1000, "   \n  ", "ab", ""):
        offsets = tokenizer.offsets(text)
        assert offsets[0] == 0 and offsets[-1] == len(text)
        assert all(later > earlier for earlier, later in pairwise(offsets))


@pytest.mark.parametrize("ratio", [1.0, 2.5, 3.5, 3.6, 4.0, 6.0])
def test_the_count_is_the_ratio_and_not_the_word_length(ratio: float) -> None:
    """``len(text) / ratio`` to within one, whatever the words look like. This is what
    makes the ratio mean "characters per token" and the calibration arithmetic hold."""
    tokenizer = ApproximateTokenizer(ratio)
    for text in (PROSE, "a " * 300, "extraordinarily " * 40, "x" * 777):
        assert abs(count(tokenizer, text) - len(text) / ratio) <= 1.0, (ratio, text[:20])


def test_boundaries_snap_to_word_starts_when_one_is_in_reach() -> None:
    offsets = ApproximateTokenizer(6.0).offsets("alpha bravo charlie delta echo")
    # "bravo" starts at 6, "charlie" at 12, "delta" at 20, "echo" at 26.
    assert offsets == [0, 6, 12, 20, 26, 30]


def test_the_name_carries_the_ratio() -> None:
    assert ApproximateTokenizer(3.6).name == "approximate:3.6"
    assert ApproximateTokenizer(4).name == "approximate:4"


def test_a_ratio_under_one_is_refused() -> None:
    with pytest.raises(ValueError):
        ApproximateTokenizer(0.9)


@pytest.mark.parametrize("strategy", ["fixed", "recursive", "by_heading", "sentence_window"])
@pytest.mark.parametrize("corpus", sorted(PATHOLOGICAL), ids=lambda name: name.replace(" ", "_"))
def test_the_approximate_tokenizer_passes_the_shared_chunking_invariants(
    strategy: str, corpus: str
) -> None:
    """The acceptance criterion: no word lost, none invented, nothing over the ceiling —
    under a tokenizer whose boundaries fall inside words, which is what makes the
    chunker's own mid-word guard load-bearing."""
    tokenizer = ApproximateTokenizer(3.6)
    extracted = PATHOLOGICAL[corpus]
    settings = config(strategy=strategy, chunk_size=100, min_chunk_size=20)

    chunks = chunk_document(extracted, settings, tokenizer=tokenizer, media_type="text/plain")

    check_invariants(chunks, extracted.text, settings, tokenizer, strategy=strategy)


def test_semantic_passes_too() -> None:
    from app.services.chunking import needs_signal

    tokenizer = ApproximateTokenizer(3.6)
    extracted = PATHOLOGICAL["prose"]
    settings = config(strategy="semantic", chunk_size=100, min_chunk_size=20)
    assert needs_signal(settings)
    chunks = chunk_document(
        extracted,
        settings,
        tokenizer=tokenizer,
        media_type="text/plain",
        signal=flat_signal(extracted, settings),
    )
    check_invariants(chunks, extracted.text, settings, tokenizer, strategy="semantic")


def test_a_fixed_cut_never_opens_mid_word_under_a_sub_word_tokenizer() -> None:
    """The case the guard exists for, in isolation: a long word straddling the window."""
    text = " ".join(["supercalifragilistic"] * 120)
    extracted = Extracted(sections=(Section(text=text, title=None),))
    for overlap in (0, 10):
        settings = ChunkingConfig.model_validate(
            {"strategy": "fixed", "chunk_size": 50, "overlap": overlap, "min_chunk_size": 0}
        )
        chunks = chunk_document(extracted, settings, tokenizer=ApproximateTokenizer(3.6))
        assert len(chunks) > 1
        for chunk in chunks:
            assert set(chunk.text.split()) == {"supercalifragilistic"}, (overlap, chunk.text)


# ---------------------------------------------------------------------------
# the fingerprint
# ---------------------------------------------------------------------------


def test_the_fingerprint_folds_in_the_tokenizer_for_every_strategy() -> None:
    for strategy in ("fixed", "recursive", "semantic"):
        settings = config(strategy=strategy)
        assert fingerprint(settings, tokenizer="o200k_base") != fingerprint(
            settings, tokenizer="approximate:3.6"
        )
        assert fingerprint(settings, tokenizer="approximate:3.6") != fingerprint(
            settings, tokenizer="approximate:3.5"
        )


def test_a_fingerprint_without_a_tokenizer_is_the_old_formula() -> None:
    """Rows written before the tokenizer was recorded still compare equal to themselves."""
    settings = config()
    assert fingerprint(settings) == fingerprint(settings, tokenizer=None)
    assert fingerprint(settings) != fingerprint(settings, tokenizer="cl100k_base")


def test_a_tokenizer_change_invalidates_every_format() -> None:
    settings = config()
    assert changed_formats(settings, settings) == frozenset()
    assert changed_formats(settings, settings, tokenizer_changed=True) >= {
        "text",
        "pdf",
        "code",
    }


# ---------------------------------------------------------------------------
# calibration
# ---------------------------------------------------------------------------


def test_the_ratio_is_provider_over_ours_summed_over_the_window() -> None:
    calibration = Calibration(estimated=1000, reported=1040, samples=12)
    assert calibration.ratio == pytest.approx(1.04)
    assert calibration.drift == pytest.approx(0.04)
    assert not calibration.warns


def test_drift_beyond_the_threshold_warns_in_both_directions() -> None:
    assert Calibration(estimated=100, reported=120, samples=1).warns
    assert Calibration(estimated=120, reported=100, samples=1).warns
    assert not Calibration(estimated=100, reported=114, samples=1).warns


def test_an_empty_window_has_no_ratio() -> None:
    empty = Calibration(estimated=0, reported=0, samples=0)
    assert empty.ratio is None and empty.drift is None and not empty.warns


def test_calibrating_scales_the_ratio_by_our_count_over_theirs() -> None:
    """Characters are estimated times old ratio; the new ratio is characters over reported.
    Undercounting by four percent means fewer characters per token."""
    proposed = calibrated(
        TokenizerSpec.approximate(3.5), Calibration(estimated=1000, reported=1040, samples=3)
    )
    assert proposed is not None
    assert proposed.ratio == pytest.approx(3.5 / 1.04, abs=0.001)


def test_only_an_approximation_can_be_calibrated() -> None:
    window = Calibration(estimated=1000, reported=1200, samples=3)
    assert calibrated(TokenizerSpec(name="o200k_base"), window) is None
    assert calibrated(TokenizerSpec.approximate(3.5), Calibration(0, 0, 0)) is None


def test_a_calibrated_ratio_stays_inside_the_bounds() -> None:
    proposed = calibrated(
        TokenizerSpec.approximate(19.0), Calibration(estimated=1000, reported=10, samples=1)
    )
    assert proposed is not None and proposed.ratio == 20.0


def test_the_drift_window_is_a_rolling_sum_per_model() -> None:
    window = DriftWindow(size=2)
    assert window.add("m", estimated=100, reported=110) == pytest.approx(1.1)
    assert window.add("m", estimated=100, reported=130) == pytest.approx(1.2)
    # The first sample falls out: (130 + 90) / 200.
    assert window.add("m", estimated=100, reported=90) == pytest.approx(1.1)
    assert window.ratio("other") is None
    assert window.add("other", estimated=0, reported=0) is None


def test_the_derivation_table_is_serialisable_for_the_ui() -> None:
    """``GET /tokenizers`` hands the table to the form; it has to survive JSON."""
    rows = [
        {"dialect": row.dialect, "prefix": row.prefix, "spec": row.spec.model_dump()}
        for row in DERIVATIONS
    ]
    assert json.loads(json.dumps(rows)) == rows
