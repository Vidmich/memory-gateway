"""The invariants every chunking strategy has to satisfy, in one place.

Written as a shared suite rather than repeated per strategy, because the value of these
is that they are the *same* four assertions everywhere. A strategy with its own weaker
version of "never mid-word" is a strategy nobody would notice had one.

**No word is lost.** Every word of the document appears in some chunk. This is the one
that catches the failure that is otherwise invisible: a splitter that skips the text
between two units indexes a document with a third of it missing, reports ``indexed``, and
answers badly forever. It is exactly why
:func:`~app.services.code_structure.declarations` returns a cover rather than a list of
declarations.

**No word is invented.** Every word in every chunk is a word of the document. This is
"never mid-word" stated so that it survives ``code``, which legitimately *rewrites* its
chunks by prefixing the enclosing declaration: the header's words are the document's words
too, whereas half of ``extraordinary`` is not.

**Nothing is empty.** An empty chunk embeds to a vector with no direction, which has
undefined similarity and matches every query that produced another one.

**Nothing exceeds the ceiling**, measured on what is actually *embedded* — which under
``sentence_window`` is the sentence and not the window around it. A chunk over the
embedding model's context is a document that can never leave the ``embedding`` status.

Indexes are checked too, and they belong here rather than in an accounting test: they are
half of the deterministic point id, so a gap leaves a stale vector behind on the next
ingestion of the same document.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.schemas.connector_config import ChunkingConfig
from app.services.chunking import Chunk
from app.services.tokenizer import Tokenizer, count

_WORD = re.compile(r"\w+")


def words(text: str) -> list[str]:
    return _WORD.findall(text)


def check_invariants(
    chunks: Sequence[Chunk],
    document: str,
    config: ChunkingConfig,
    tokenizer: Tokenizer,
    *,
    strategy: str = "",
) -> None:
    """Assert all five. ``strategy`` only names the failure."""
    where = f" [{strategy or config.strategy}]"

    assert [chunk.index for chunk in chunks] == list(range(len(chunks))), (
        f"chunk indexes are not contiguous from zero{where}"
    )

    for chunk in chunks:
        assert chunk.text.strip(), f"an empty chunk was produced{where}"
        assert chunk.embedded_text.strip(), f"a chunk with nothing to embed{where}"
        embedded = count(tokenizer, chunk.embedded_text)
        assert embedded <= config.chunk_size, (
            f"a chunk of {embedded} tokens exceeds the {config.chunk_size} ceiling{where}"
        )

    produced = set()
    for chunk in chunks:
        produced.update(words(chunk.text))
    original = set(words(document))

    invented = produced - original
    assert not invented, f"words that are not in the document: {sorted(invented)[:5]}{where}"

    lost = original - produced
    assert not lost, f"words of the document that reached no chunk: {sorted(lost)[:5]}{where}"


__all__ = ["check_invariants", "words"]
