"""Golden-file comparison for assembled prompts.

The assembled system message is the product surface SPEC §7 specifies down to the
punctuation, and the failure mode this guards against is not a crash — it is somebody
"tidying" the citation instruction, or a stray blank line appearing between layers,
and nothing noticing because every assertion was about lengths and substrings.

So the whole rendered message goes into a file and is compared byte for byte. Running the
suite with ``UPDATE_GOLDEN=1`` rewrites them, which makes an intentional change one
command and a diff to read in review — and makes an *unintentional* one a failing test
with the difference printed.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from app.services.retrieval import Chunk

GOLDEN_DIR = Path(__file__).parent / "golden"

#: Set to rewrite the files instead of asserting against them.
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"


def assert_golden(name: str, actual: str) -> None:
    path = GOLDEN_DIR / f"{name}.txt"
    if UPDATE:  # pragma: no cover - only under an explicit env var
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        return
    assert path.exists(), (
        f"no golden file {path.name}; run the suite with UPDATE_GOLDEN=1 to create it"
    )
    expected = path.read_text(encoding="utf-8")
    assert actual == expected, (
        f"the assembled prompt no longer matches {path.name}. If the change is "
        f"intended, re-run with UPDATE_GOLDEN=1 and review the diff."
    )


def chunk(
    text: str,
    *,
    score: float = 0.8,
    source: str = "handbook.md",
    section: str | None = None,
    document: str = "11111111-1111-5111-8111-111111111111",
    index: int = 0,
) -> Chunk:
    """A retrieved chunk, with stable ids so a golden file is reproducible."""
    return Chunk(
        id=f"{document}:{index}",
        score=score,
        text=text,
        source_name=source,
        page_or_section=section,
        document_id=document,
        connector_id=str(uuid.UUID(int=7)),
        chunk_index=index,
    )


__all__ = ["GOLDEN_DIR", "UPDATE", "assert_golden", "chunk"]
