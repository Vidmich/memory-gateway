"""Token counting, behind a port.

SPEC §9.3 measures chunks in tokens, and the reason is not pedantry: a chunk sized in
characters is a different size in every language, and the one place that matters is the
embedding model's context window, which is counted in tokens or not at all.

The port is deliberately *offsets*, not counts. A counter alone can answer "how big is
this chunk" but not "where does the 1000th token start", and the second question is the
one a splitter actually asks. Working in character offsets throughout means the chunker
can snap a boundary to a paragraph break and still know exactly how many tokens it just
took, without a second encode of every candidate.

Three implementations. :class:`TiktokenCounter` is the real BPE, for the models whose
vocabulary ships with ``tiktoken``. :class:`ApproximateTokenizer` is a characters-per-token
ratio for the models whose vocabulary does not — Claude, Llama, Mistral, a self-hosted
embedding endpoint — calibrated against the count the provider reports (task 101).
:class:`WordTokenizer` is the fallback, and it exists because ``tiktoken`` fetches its
vocabulary over the network on first use: an air-gapped deployment, a locked-down CI
runner, or a cold container with no egress would otherwise turn a missing download into a
worker that cannot ingest anything. Degrading to an approximate count and saying so — in
the log, and since task 101 in the tokenizer's own ``name`` — is the better failure:
chunks come out roughly 30% larger than asked for, which costs retrieval quality, where
the alternative costs the whole feature.

Which of the three a given count uses is decided in :mod:`app.services.tokenizers`, from
the model the tokens are for. Nothing in this module chooses.
"""

from __future__ import annotations

import bisect
import logging
import math
import re
import threading
from typing import Protocol

logger = logging.getLogger(__name__)

#: The encoding OpenAI's `text-embedding-3-*` models use. Named rather than derived from
#: the model id, because `encoding_for_model` raises for any model it has not heard of —
#: which includes every self-hosted and third-party embedding endpoint.
DEFAULT_ENCODING = "cl100k_base"


class Tokenizer(Protocol):
    """Where tokens begin, in character offsets."""

    @property
    def name(self) -> str:
        """Recorded alongside chunk counts, so a later change of tokenizer is visible
        rather than a mysterious shift in chunk sizes."""
        ...

    def offsets(self, text: str) -> list[int]:
        """The character offset each token starts at, followed by ``len(text)``.

        Always non-empty and always ends with the text length, so
        ``len(offsets) - 1`` is the token count and ``offsets[i]`` is a valid slice
        boundary for every ``i``. Both properties are what let the chunker index into it
        without a special case for the empty string.
        """
        ...


def count(tokenizer: Tokenizer, text: str) -> int:
    """Tokens in ``text``. A free function rather than a method: it is derivable from
    :meth:`Tokenizer.offsets`, and an implementation that could disagree with its own
    offsets would produce chunks whose reported size is not their real one."""
    return len(tokenizer.offsets(text)) - 1


class WordTokenizer:
    """Whitespace-and-punctuation approximation. No vocabulary, no network, no surprises.

    Punctuation is split out because BPE does the same and because a chunk boundary
    landing between a word and its full stop is harmless, whereas one landing inside a
    word is not.
    """

    name = "words"

    _PATTERN = re.compile(r"\w+|[^\w\s]", re.UNICODE)

    def offsets(self, text: str) -> list[int]:
        found = [match.start() for match in self._PATTERN.finditer(text)]
        return [*found, len(text)]


class ApproximateTokenizer:
    """A token every ``ratio`` characters, snapped to the nearest word start.

    The count is the point and the snapping is a courtesy. The count is
    ``len(text) / ratio`` to within one, whatever the text, because that is the quantity
    the calibration measures and corrects — a snap that merged two boundaries into one
    word start would make the count depend on word length and the ratio stop meaning
    "characters per token". So the reach is *under half a token*: two ideal offsets can
    never both snap to the same word start, and every ideal produces exactly one token.
    Within that reach a boundary moves to the start of a word, which is where a BPE's
    boundaries mostly fall anyway.

    It follows that boundaries *do* land inside words — any tokenizer whose tokens are
    shorter than words has that property, and ``cl100k_base`` splits ``extraordinary`` in
    two as well. The chunker, not the tokenizer, is what keeps a chunk from opening
    mid-word: see ``_off_word`` in :mod:`app.services.chunking`.

    The ratio is part of the name because two approximations with different ratios cut
    different chunks, and the chunk fingerprint has to say so.
    """

    def __init__(self, ratio: float) -> None:
        if not ratio >= 1:
            raise ValueError("characters per token must be at least one")
        self._ratio = float(ratio)
        #: Strictly under half the smallest spacing between two ideals (which is
        #: ``floor(ratio)``, since ideals are truncated), so no two of them can reach the
        #: same word start — which is what keeps the count exact.
        self._reach = max(0, (math.floor(self._ratio) - 1) // 2)

    @property
    def ratio(self) -> float:
        return self._ratio

    @property
    def name(self) -> str:
        return f"approximate:{self._ratio:g}"

    def offsets(self, text: str) -> list[int]:
        length = len(text)
        if length == 0:
            return [0]
        starts = [0]
        target = self._ratio
        while target < length:
            ideal = int(target)
            snapped = self._word_start_near(text, ideal)
            position = ideal if snapped is None else snapped
            # Two ideals can snap to the same word start; a repeated offset would be a
            # zero-width token, which the chunker turns into an empty chunk.
            if position > starts[-1]:
                starts.append(position)
            target += self._ratio
        return [*starts, length]

    def _word_start_near(self, text: str, position: int) -> int | None:
        """The closest offset within reach that begins a word, if any."""
        if self._reach == 0:
            return None
        best: int | None = None
        low = max(1, position - self._reach)
        high = min(len(text) - 1, position + self._reach)
        for candidate in range(low, high + 1):
            starts_word = text[candidate - 1].isspace() and not text[candidate].isspace()
            if starts_word and (best is None or abs(candidate - position) < abs(best - position)):
                best = candidate
        return best


class TiktokenCounter:
    """The real BPE, loaded once per process and shared.

    The vocabulary load is lazy and guarded by a lock: `tiktoken` fetches and caches it on
    first use, and a worker starting eight jobs at once should make one download, not
    eight. A failure is recorded so the fallback is not re-attempted on every document.

    A counter that had to fall back says so in its :attr:`name` — ``words (cl100k_base
    unavailable)`` — so the degradation lands on the document row and on the model page
    instead of only in a log line. A row claiming ``cl100k_base`` for chunks that were cut
    by word count would be a wrong fingerprint that agrees with itself.
    """

    def __init__(self, encoding_name: str = DEFAULT_ENCODING) -> None:
        self._encoding_name = encoding_name
        self._encoding: object | None = None
        self._unavailable = False
        self._lock = threading.Lock()
        self._fallback = WordTokenizer()

    @property
    def name(self) -> str:
        if self._load() is not None:
            return self._encoding_name
        return f"{self._fallback.name} ({self._encoding_name} unavailable)"

    @property
    def degraded(self) -> bool:
        """Whether the vocabulary failed to load and counts are the word fallback's."""
        return self._load() is None

    def offsets(self, text: str) -> list[int]:
        encoding = self._load()
        if encoding is None:
            return self._fallback.offsets(text)
        return _offsets_from_encoding(encoding, text)

    def _load(self) -> object | None:
        if self._settled():
            return self._encoding
        with self._lock:
            if self._settled():
                return self._encoding
            try:
                import tiktoken

                self._encoding = tiktoken.get_encoding(self._encoding_name)
            except Exception as exc:
                # Every failure mode is the same failure mode from here: no vocabulary.
                # Not raising is the point — see the module docstring.
                self._unavailable = True
                logger.warning(
                    "tiktoken is unavailable; token counts are approximate",
                    extra={"encoding": self._encoding_name, "error": str(exc)},
                )
            return self._encoding

    def _settled(self) -> bool:
        """Whether the one-time load has already happened, either way."""
        return self._encoding is not None or self._unavailable


def _offsets_from_encoding(encoding: object, text: str) -> list[int]:
    """Map BPE token boundaries back to character offsets.

    tiktoken works in bytes and offers no offset API, so each token's UTF-8 length is
    accumulated and the running byte offset is translated into a character offset. BPE
    operates on raw bytes, so a token boundary can in principle land inside a multi-byte
    character; such a boundary is snapped forward to the next character, which merges two
    tokens for splitting purposes and never produces an invalid slice.
    """
    if not text:
        return [0]

    ids = encoding.encode(text, disallowed_special=())  # type: ignore[attr-defined]
    raw = text.encode("utf-8")
    # Character index for each byte position that starts a character; the final entry
    # maps the end of the buffer to the end of the string.
    char_at_byte: list[int] = []
    for index, character in enumerate(text):
        char_at_byte.extend([index] * len(character.encode("utf-8")))
    char_at_byte.append(len(text))

    starts: list[int] = []
    byte_offset = 0
    previous = -1
    for token in ids:
        position = char_at_byte[min(byte_offset, len(raw))]
        # A boundary inside a character maps to the same char index as the previous
        # token; keeping both would produce a zero-width token and an empty chunk.
        if position != previous:
            starts.append(position)
            previous = position
        byte_offset += len(encoding.decode_single_token_bytes(token))  # type: ignore[attr-defined]

    return [*starts, len(text)]


def token_span(offsets: list[int], start: int, tokens: int) -> int:
    """The character offset ``tokens`` tokens after character offset ``start``.

    ``tokens`` may be negative, which is how overlap is expressed: the next chunk begins
    a fixed number of tokens *before* the previous one ended. Clamped at both ends, so a
    caller never has to check whether its overlap ran off the front of the document.
    """
    target = token_index(offsets, start) + tokens
    return offsets[min(max(target, 0), len(offsets) - 1)]


def token_index(offsets: list[int], position: int) -> int:
    """How many tokens precede a character offset."""
    return max(0, bisect.bisect_right(offsets, position) - 1)


def build_tokenizer(name: str = DEFAULT_ENCODING) -> Tokenizer:
    return TiktokenCounter(name)


__all__ = [
    "DEFAULT_ENCODING",
    "ApproximateTokenizer",
    "TiktokenCounter",
    "Tokenizer",
    "WordTokenizer",
    "build_tokenizer",
    "count",
    "token_index",
    "token_span",
]
