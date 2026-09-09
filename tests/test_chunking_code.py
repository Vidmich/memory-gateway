"""``code``: declarations as units, and the degradations that must never fail a document.

Two layers, tested separately on purpose.
:mod:`app.services.code_structure` answers "what are the units of this file" and is checked
against source text; :mod:`app.services.chunking` decides which units fit and which have to
be descended into, and is checked against sizes. A single test that asserted both would
fail for two unrelated reasons and say neither.

The claim this file exists to keep honest is the one in the UI: *code-aware for Python,
JavaScript, TypeScript and Go; recursive elsewhere*. "Code-aware" is not a claim anybody
can check, and that sentence is.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from app.schemas.connector_config import ChunkingConfig
from app.services.chunking import chunk_document
from app.services.code_structure import MAX_CODE_CHARS, declarations
from app.services.extraction import Extracted, Section
from app.services.filetypes import CODE_LANGUAGE_NAMES, CODE_LANGUAGES, language_of
from app.services.tokenizer import Tokenizer, WordTokenizer
from tests.chunking_contract import check_invariants

TOKENIZER: Tokenizer = WordTokenizer()

PYTHON = '''"""Module docstring."""

import os


def alpha(value):
    """Doc."""
    return value + 1


class Handler:
    """A class."""

    limit = 10

    def serve(self, request):
        """Handle one request and return whatever it decided."""
        checked = self.check(request)
        prepared = self.prepare(checked)
        return self.respond(prepared, limit=self.limit)

    def close(self):
        """Release everything this handler is holding on to."""
        self.pending.clear()
        self.open_sockets.clear()
        return None


CONSTANT = 3
'''

TYPESCRIPT = """import { thing } from './thing'

export function alpha(a: number): string {
  return `not a } brace ${a}`
}

export class Handler {
  constructor(private options: Options) {}

  async serve(request: Request) {
    if (request) { return 1 }
    return 2
  }
}

export const bravo = (n: number) => n * 2

export type Result = string
"""

GO = """package main

import "fmt"

type Server struct {
\tname string
}

func (s *Server) Serve(port int) error {
\tfmt.Println("}")
\treturn nil
}

func main() {
\tfmt.Println("hi")
}
"""


def one(text: str) -> Extracted:
    return Extracted(sections=(Section(text=text),))


def names(text: str, language: str) -> list[str | None]:
    return [declaration.name for declaration in declarations(text, language)]


# ---------------------------------------------------------------------------
# the structure layer
# ---------------------------------------------------------------------------


def test_python_declarations_are_found_with_their_nesting() -> None:
    found = declarations(PYTHON, "python")

    assert names(PYTHON, "python") == [None, "alpha", "Handler", None]
    handler = next(one for one in found if one.name == "Handler")
    assert [child.name for child in handler.children] == ["Handler.serve", "Handler.close"]
    assert handler.header == "class Handler:"


def test_the_units_cover_the_whole_file() -> None:
    """The invariant the cover exists for: a parser that returned only the declarations
    would silently drop the imports and the module-level constants, and the file would
    index with a third of it missing."""
    found = declarations(PYTHON, "python")

    assert found[0].start == 0
    assert found[-1].end == len(PYTHON)
    assert all(one.end == other.start for one, other in pairwise(found))


def test_a_brace_inside_a_string_does_not_end_a_function() -> None:
    """Without the string mask, one ``}`` in a log message ends a function two hundred
    lines early and every chunk after it is misattributed."""
    found = declarations(TYPESCRIPT, "typescript")

    alpha = next(one for one in found if one.name == "alpha")
    assert "return `not a } brace" in TYPESCRIPT[alpha.start : alpha.end]
    assert TYPESCRIPT[alpha.start : alpha.end].count("export") == 1


def test_typescript_class_members_come_back_as_children() -> None:
    found = declarations(TYPESCRIPT, "typescript")

    handler = next(one for one in found if one.name == "Handler")
    assert [child.name for child in handler.children] == ["Handler.constructor", "Handler.serve"]


def test_a_go_method_is_named_for_its_receiver() -> None:
    """``Server.Serve``, not ``Serve``. A citation naming a bare method in a package with
    four types is a citation nobody can place."""
    assert names(GO, "go") == [None, "Server", "Server.Serve", "main"]


def test_a_syntax_error_produces_no_structure_rather_than_an_exception() -> None:
    """A broken file must cost a worse chunking of that file, never a failed document."""
    assert declarations("def broken(:\n    pass\n", "python") == ()


def test_unbalanced_braces_are_treated_as_unparseable() -> None:
    assert declarations("function alpha() {\n  return 1\n", "javascript") == ()


def test_a_minified_bundle_has_no_declarations_to_find() -> None:
    """Every declaration on line one, so nothing matches at column zero, so it falls
    through to ``recursive`` — which is the right answer for a generated bundle."""
    minified = "function a(){return 1}function b(){return 2}var c=1;" * 20

    assert declarations(minified, "javascript") == ()


def test_a_language_nothing_here_parses_is_not_pretended_to_be_parsed() -> None:
    assert declarations('fn main() { println!("hi") }', "rust") == ()
    assert language_of("text/x-rust") is None


def test_a_very_large_file_is_not_scanned() -> None:
    """The scanner is a character loop. A file this size is a generated bundle rather than
    something whose functions anybody wants cited, and both halves point the same way."""
    assert declarations("def alpha():\n    pass\n" + "# pad\n" * MAX_CODE_CHARS, "python") == ()


def test_the_named_languages_are_the_ones_that_actually_parse() -> None:
    """The sentence in the UI, checked. If a language is added to the mapping and not to
    the sentence — or the other way round — this is what says so."""
    parsed = {language_of(media) for media in CODE_LANGUAGES}

    assert parsed == {name.lower() for name in CODE_LANGUAGE_NAMES}
    for language in parsed:
        assert language is not None
        source = {"python": PYTHON, "go": GO}.get(language, TYPESCRIPT)
        assert declarations(source, language), f"{language} claims support and parses nothing"


# ---------------------------------------------------------------------------
# the strategy
# ---------------------------------------------------------------------------


def test_chunk_boundaries_are_declarations() -> None:
    chunks = chunk_document(
        one(PYTHON),
        ChunkingConfig(strategy="code", chunk_size=1000, overlap=0),
        tokenizer=TOKENIZER,
        media_type="text/x-python",
    )

    assert [chunk.section for chunk in chunks] == [None, "alpha", "Handler", None]
    assert chunks[1].text.startswith("def alpha(value):")


def test_a_small_class_stays_whole() -> None:
    """Its methods are about each other. Splitting a forty-line class into six chunks makes
    six worse answers out of one good one."""
    chunks = chunk_document(
        one(PYTHON),
        ChunkingConfig(strategy="code", chunk_size=1000, overlap=0),
        tokenizer=TOKENIZER,
        media_type="text/x-python",
    )

    handler = next(chunk for chunk in chunks if chunk.section == "Handler")
    assert "def serve" in handler.text
    assert "def close" in handler.text


def test_an_oversized_class_is_split_by_method_and_keeps_its_signature() -> None:
    """A fragment of a class body with no ``class`` line above it is a citation nobody can
    place — and the citation naming the function is the demo this task is about."""
    chunks = chunk_document(
        one(PYTHON),
        ChunkingConfig(strategy="code", chunk_size=50, overlap=0),
        tokenizer=TOKENIZER,
        media_type="text/x-python",
    )

    methods = [chunk for chunk in chunks if chunk.section in ("Handler.serve", "Handler.close")]
    assert methods, "an oversized class should be descended into"
    assert all(chunk.text.startswith("class Handler:") for chunk in methods)


def test_a_file_that_will_not_parse_still_chunks() -> None:
    """The degradation that matters. A syntax error in one file of a repository costs a
    worse cut of that file and nothing else."""
    broken = "def broken(:\n" + "    still text here\n" * 50

    chunks = chunk_document(
        one(broken),
        ChunkingConfig(strategy="code", chunk_size=60, overlap=0),
        tokenizer=TOKENIZER,
        media_type="text/x-python",
    )

    assert len(chunks) > 1
    settings = ChunkingConfig(strategy="code", chunk_size=60, overlap=0)
    check_invariants(chunks, broken, settings, TOKENIZER)


@pytest.mark.parametrize(
    ("source", "media_type"),
    [(PYTHON, "text/x-python"), (TYPESCRIPT, "text/x-typescript"), (GO, "text/x-go")],
)
def test_nothing_is_lost_or_invented_in_any_supported_language(
    source: str, media_type: str
) -> None:
    settings = ChunkingConfig(strategy="code", chunk_size=100, overlap=0)

    chunks = chunk_document(one(source), settings, tokenizer=TOKENIZER, media_type=media_type)

    check_invariants(chunks, source, settings, TOKENIZER, strategy=f"code/{media_type}")


def test_a_markdown_file_under_the_code_strategy_falls_back_rather_than_failing() -> None:
    """A connector is a source, not a format. Setting ``code`` on a repository that also
    holds a README must not make the README a problem — which is what per-format overrides
    are for, and what this guarantees when nobody sets one."""
    prose = "# Title\n\n" + "Some ordinary prose here. " * 40

    chunks = chunk_document(
        one(prose),
        ChunkingConfig(strategy="code", chunk_size=60, overlap=0),
        tokenizer=TOKENIZER,
        media_type="text/markdown",
    )

    assert len(chunks) > 1
