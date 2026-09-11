"""Which tokenizer, for which model (task 101).

There used to be one tokenizer in the process — ``cl100k_base``, handed to the chunker,
the prompt assembler and the rate limiter alike. It is the encoding of ``gpt-4`` and
``text-embedding-3-*``, and of nothing else anyone routes to: ``gpt-4o`` is ``o200k_base``,
Claude's is not published, Llama's and Mistral's are downloads with licence gates. A chunk
sized for the wrong model either wastes a slice of the embedding window or is truncated by
the provider without a word; a budget enforced in the wrong unit is right for at most one
of a gateway's targets. This module is the answer to "which tokenizer", asked once.

**Derived unless overridden** is the whole design. :func:`derive` maps a dialect and a
model id onto the best tokenizer that ships, so nobody has to know what ``o200k_base`` is
to get correct counts for ``gpt-4o``; an override lets anyone running something the table
has not heard of say what it is. Both show their origin, because the day a derivation is
wrong for a new model the fastest fix is an override and the fastest diagnosis is seeing
that none is set.

**The registry is closed.** A tokenizer name is one of five strings, validated where it is
stored, so a misspelled override is a 422 and never a silent fall to word counts. The
``approximate`` entry is the honest one: it is a characters-per-token ratio, and the ratio
is measured — :class:`Calibration` compares our count against the ``prompt_tokens`` the
provider reports, which is the one count that is authoritative, and turns an approximation
from a lie into an estimate with an error bar.

Loading a BPE vocabulary is expensive, so :func:`resolve` caches per specification. The
objects are stateless once loaded, and a worker starting eight jobs at once should make
one download, not eight.
"""

from __future__ import annotations

import functools
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.tokenizer import (
    ApproximateTokenizer,
    TiktokenCounter,
    Tokenizer,
    WordTokenizer,
)

APPROXIMATE = "approximate"
WORDS = "words"
#: The encodings ``tiktoken`` ships. Vocabularies for anything else are a download per
#: model, some behind licence gates — see the task's out-of-scope note.
TIKTOKEN_ENCODINGS: tuple[str, ...] = ("cl100k_base", "o200k_base", "p50k_base")
TOKENIZER_NAMES: tuple[str, ...] = (*TIKTOKEN_ENCODINGS, APPROXIMATE, WORDS)

type TokenizerName = Literal["cl100k_base", "o200k_base", "p50k_base", "approximate", "words"]

#: Characters per token. One is a tokenizer that counts characters; twenty is one that
#: counts sentences. Nothing real is outside the range, and a typo usually is.
MIN_RATIO = 1.0
MAX_RATIO = 20.0
#: The rule of thumb for English under a modern BPE, and what a model nobody has heard of
#: starts on.
DEFAULT_RATIO = 4.0
#: Claude's tokenizer runs a little denser than OpenAI's on the same English text. The
#: number is a starting point the calibration is expected to move.
CLAUDE_RATIO = 3.5

ORIGIN_DERIVED = "derived"
ORIGIN_OVERRIDE = "override"

#: Our count versus the provider's, beyond which the model page and the gateway that
#: routes to it show a warning. A configuration problem with a one-click fix, so a
#: warning on the screen and not an alert on the pager.
DRIFT_WARNING = 0.15


class TokenizerSpec(BaseModel):
    """The stored form: a name from the closed registry, and a ratio for ``approximate``.

    ``approximate`` requires the ratio and every other name rejects one, both as
    validation errors — a ratio on ``o200k_base`` is a misunderstanding worth stopping,
    not a field to ignore.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: TokenizerName
    ratio: float | None = Field(default=None, ge=MIN_RATIO, le=MAX_RATIO)

    @model_validator(mode="after")
    def _ratio_matches_name(self) -> Self:
        if self.name == APPROXIMATE and self.ratio is None:
            raise ValueError("the approximate tokenizer needs 'ratio' (characters per token)")
        if self.name != APPROXIMATE and self.ratio is not None:
            raise ValueError(f"'{self.name}' is a fixed vocabulary and takes no ratio")
        return self

    @classmethod
    def approximate(cls, ratio: float) -> TokenizerSpec:
        return cls(name=APPROXIMATE, ratio=round(float(ratio), 3))

    @classmethod
    def parse(cls, key: str) -> TokenizerSpec:
        """The inverse of :attr:`key`. What the gateway cache payload carries."""
        name, _, ratio = key.partition(":")
        return cls.model_validate({"name": name, "ratio": float(ratio) if ratio else None})

    @property
    def key(self) -> str:
        """``o200k_base`` or ``approximate:3.5`` — one string that names the tokenizer
        exactly, including the ratio, because two approximations with different ratios
        cut different chunks."""
        if self.name == APPROXIMATE:
            return f"{APPROXIMATE}:{self.ratio:g}"
        return self.name


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


def _approximate(spec: TokenizerSpec) -> Tokenizer:
    assert spec.ratio is not None  # the validator guarantees it
    return ApproximateTokenizer(spec.ratio)


TOKENIZERS: Mapping[str, Callable[[TokenizerSpec], Tokenizer]] = {
    **{encoding: (lambda spec: TiktokenCounter(spec.name)) for encoding in TIKTOKEN_ENCODINGS},
    APPROXIMATE: _approximate,
    WORDS: lambda spec: WordTokenizer(),
}


@functools.cache
def _resolve(key: str) -> Tokenizer:
    spec = TokenizerSpec.parse(key)
    return TOKENIZERS[spec.name](spec)


def resolve(spec: TokenizerSpec | str) -> Tokenizer:
    """The tokenizer for a specification, built once per process and shared."""
    return _resolve(spec if isinstance(spec, str) else spec.key)


# ---------------------------------------------------------------------------
# derivation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Derivation:
    """One row of the preset table: models under ``dialect`` whose id starts with
    ``prefix`` use ``spec``. ``dialect`` ``None`` matches every dialect; an empty prefix
    matches every model of the dialect, which is how a Claude endpoint claims its whole
    namespace."""

    dialect: str | None
    prefix: str
    spec: TokenizerSpec


_O200K = TokenizerSpec(name="o200k_base")
_CL100K = TokenizerSpec(name="cl100k_base")
_P50K = TokenizerSpec(name="p50k_base")
_CLAUDE = TokenizerSpec.approximate(CLAUDE_RATIO)

#: Longest matching prefix wins, so ``gpt-4o`` beats ``gpt-4``. The table is the single
#: source of the knowledge: the API serves it to the UI (``GET /tokenizers``), so the form
#: can show the derived value while somebody types without a second copy that would
#: disagree with this one within a month.
DERIVATIONS: tuple[Derivation, ...] = (
    Derivation("openai", "gpt-4o", _O200K),
    Derivation("openai", "chatgpt-4o", _O200K),
    Derivation("openai", "gpt-4.1", _O200K),
    Derivation("openai", "gpt-5", _O200K),
    Derivation("openai", "o1", _O200K),
    Derivation("openai", "o3", _O200K),
    Derivation("openai", "o4", _O200K),
    Derivation("openai", "gpt-4", _CL100K),
    Derivation("openai", "gpt-3.5", _CL100K),
    Derivation("openai", "text-embedding-3", _CL100K),
    Derivation("openai", "text-embedding-ada", _CL100K),
    Derivation("openai", "text-davinci", _P50K),
    Derivation("openai", "code-davinci", _P50K),
    Derivation(None, "claude", _CLAUDE),
    Derivation("anthropic", "", _CLAUDE),
)

#: What anything the table does not name gets: Llama, Mistral, Qwen, a self-hosted
#: embedding endpoint. Calibration is what makes this number honest.
FALLBACK = TokenizerSpec.approximate(DEFAULT_RATIO)


def derive(dialect: str, model_id: str) -> TokenizerSpec:
    """The tokenizer a model most likely uses, from what the catalog knows about it.

    The model id is matched both whole and after its last ``/``, because OpenRouter and
    Together namespace ids by vendor — ``openai/gpt-4o-mini`` is ``gpt-4o-mini``.
    """
    candidates = {model_id.strip().lower()}
    candidates.add(model_id.strip().lower().rsplit("/", 1)[-1])
    best: Derivation | None = None
    for row in DERIVATIONS:
        if row.dialect is not None and row.dialect != dialect:
            continue
        if not any(candidate.startswith(row.prefix) for candidate in candidates):
            continue
        if best is None or len(row.prefix) > len(best.prefix):
            best = row
    return best.spec if best is not None else FALLBACK


@dataclass(frozen=True, slots=True)
class Effective:
    """A resolved choice, with where it came from.

    ``spec`` is what was decided; :attr:`name` is what the tokenizer *says it is*, which
    differs exactly when a vocabulary failed to load — ``words (cl100k_base unavailable)``
    — and that difference is the whole reason the name is recorded per document.
    """

    spec: TokenizerSpec
    origin: str

    @property
    def key(self) -> str:
        return self.spec.key

    @property
    def tokenizer(self) -> Tokenizer:
        return resolve(self.spec)

    @property
    def name(self) -> str:
        return self.tokenizer.name

    @property
    def degraded(self) -> bool:
        return self.name != self.spec.key

    @property
    def overridden(self) -> bool:
        return self.origin == ORIGIN_OVERRIDE

    @property
    def approximate(self) -> bool:
        return self.spec.name == APPROXIMATE

    def label(self) -> str:
        """``o200k_base (derived)`` — what the model page and the document list print."""
        return f"{self.name} ({self.origin})"


def effective(dialect: str, model_id: str, override: TokenizerSpec | None) -> Effective:
    """Derived unless overridden. The one function every consumer goes through."""
    if override is not None:
        return Effective(spec=override, origin=ORIGIN_OVERRIDE)
    return Effective(spec=derive(dialect, model_id), origin=ORIGIN_DERIVED)


def stored(value: Mapping[str, object] | None) -> TokenizerSpec | None:
    """An override as the database holds it — a JSON object or ``NULL``.

    Permissive on the way *out* of the row, like every configuration blob: a value that
    no longer validates (a name this build dropped) reads as "no override" rather than
    taking the model down, and the control plane refuses to write one in the first place.
    """
    if not value:
        return None
    try:
        return TokenizerSpec.model_validate(dict(value))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# calibration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Calibration:
    """Our count against the provider's, summed over a window of requests.

    Sums rather than a mean of per-request ratios, so a long prompt weighs what it costs
    and a three-token "hi" does not get a vote the size of a document.
    """

    estimated: int
    reported: int
    samples: int

    @property
    def ratio(self) -> float | None:
        """Provider ÷ ours. ``1.04`` means we undercount by four percent."""
        if self.estimated <= 0 or self.samples <= 0:
            return None
        return self.reported / self.estimated

    @property
    def drift(self) -> float | None:
        ratio = self.ratio
        return None if ratio is None else abs(ratio - 1.0)

    @property
    def warns(self) -> bool:
        drift = self.drift
        return drift is not None and drift > DRIFT_WARNING


def calibrated(spec: TokenizerSpec, calibration: Calibration) -> TokenizerSpec | None:
    """The ``approximate`` ratio that would have matched the provider over the window.

    Characters are estimated × old ratio; the new ratio is characters ÷ reported. Only an
    approximation can be moved this way — a BPE's count is what it is, and the fix for a
    drifting BPE is a different tokenizer, not a scaled one. ``None`` when there is
    nothing to calibrate from.
    """
    ratio = calibration.ratio
    if spec.name != APPROXIMATE or spec.ratio is None or ratio is None:
        return None
    proposed = spec.ratio / ratio
    return TokenizerSpec.approximate(min(MAX_RATIO, max(MIN_RATIO, proposed)))


class DriftWindow:
    """A per-model rolling window of ``(estimated, reported)`` pairs, for the gauge.

    In-process and bounded, because a gauge is a process-local number: the authoritative
    window is the request log, which :class:`~app.services.metrics_store.MetricsTransaction`
    reads for the screen. This exists so a deployment that wants an alert on drift can
    scrape one without a query.
    """

    def __init__(self, size: int = 256) -> None:
        self._size = size
        self._windows: dict[str, deque[tuple[int, int]]] = {}
        self._lock = threading.Lock()

    def add(self, model: str, *, estimated: int, reported: int) -> float | None:
        """Record a sample and return the window's current ratio."""
        with self._lock:
            window = self._windows.setdefault(model, deque(maxlen=self._size))
            window.append((estimated, reported))
            return self._ratio(window)

    def ratio(self, model: str) -> float | None:
        with self._lock:
            window = self._windows.get(model)
            return self._ratio(window) if window else None

    @staticmethod
    def _ratio(window: deque[tuple[int, int]]) -> float | None:
        estimated = sum(pair[0] for pair in window)
        reported = sum(pair[1] for pair in window)
        return reported / estimated if estimated > 0 else None


__all__ = [
    "APPROXIMATE",
    "CLAUDE_RATIO",
    "DEFAULT_RATIO",
    "DERIVATIONS",
    "DRIFT_WARNING",
    "FALLBACK",
    "MAX_RATIO",
    "MIN_RATIO",
    "ORIGIN_DERIVED",
    "ORIGIN_OVERRIDE",
    "TIKTOKEN_ENCODINGS",
    "TOKENIZERS",
    "TOKENIZER_NAMES",
    "WORDS",
    "Calibration",
    "Derivation",
    "DriftWindow",
    "Effective",
    "TokenizerSpec",
    "calibrated",
    "derive",
    "effective",
    "resolve",
    "stored",
]
