"""Where a source file's declarations begin and end, for the ``code`` chunking strategy.

Structure, not sizes. This module answers "what are the units of this file"; deciding
which of them fit in a chunk and which have to be descended into belongs to
:mod:`app.services.chunking`, which is the only thing here that knows about tokenizers.
Keeping the two apart is what lets the parsers be tested against source files and the
splitter be tested against numbers.

**The output is a cover, not a list of highlights.** Every character of the file lands in
exactly one unit: the declarations, plus filler units for the imports at the top, the
constants between two functions, and the trailing newline at the bottom. A parser that
returned only the declarations would silently drop everything between them, and the loss
would be invisible — the file would index, with a third of it missing.

**Failure means an empty result, never an exception.** A syntax error, a language nothing
here parses, a minified bundle whose declarations are all on line 1: each returns ``()``
and the caller falls back to ``recursive``. A worse chunking of one file is an acceptable
outcome; a ``failed`` document because somebody committed a broken file is not.

Four languages, and they are named in the UI rather than implied. "Code-aware" is not a
claim anybody can check; "code-aware for Python, JavaScript, TypeScript and Go" is —
see :data:`~app.services.filetypes.CODE_LANGUAGE_NAMES`.

Python goes through :mod:`ast`, which is exact. The other three go through a brace scanner
that understands strings and comments well enough not to be fooled by a ``{`` inside one.
That is a deliberate stop short of a real parser: the failure mode of the scanner is a
unit boundary in the wrong place, which costs retrieval quality on one file, whereas
adding a grammar per language is a dependency and a build step per language.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

#: Above this, structural splitting is not attempted. The scanner is a character loop in
#: Python, and a source file this size is a generated bundle rather than something whose
#: functions anybody wants cited — both halves of which point at the same answer.
MAX_CODE_CHARS = 400_000

#: A declaration whose opening line is longer than this is not usable structure. The case
#: is a minified bundle: ``function a(){...}function b(){...}`` all on line one matches the
#: declaration pattern perfectly, and the result is one "declaration" covering the file
#: whose "signature" is five kilobytes of code — carried, by the strategy, into the top of
#: every fragment cut out of it. One overlong line is enough to call the file generated.
MAX_DECLARATION_LINE = 500


@dataclass(frozen=True, slots=True)
class Declaration:
    """One unit of a source file.

    ``header`` is the line a fragment needs to keep its meaning — ``class Handler:``,
    ``func (s *Server) Serve(...)``. It is carried into every chunk cut out of an oversized
    body, because a fragment of a function with no signature above it is a citation nobody
    can place.

    ``name`` is ``None`` for filler: the imports at the top of a file are not a declaration
    and labelling them as one would put an invented function name in a citation.
    """

    start: int
    end: int
    header: str
    name: str | None
    children: tuple[Declaration, ...] = ()


def declarations(text: str, language: str) -> tuple[Declaration, ...]:
    """The units of ``text``, covering it completely. ``()`` if it cannot be parsed."""
    if not text.strip() or len(text) > MAX_CODE_CHARS:
        return ()
    if language == "python":
        return _python(text)
    if language in ("javascript", "typescript", "go"):
        return _braced(text, language)
    return ()


# ---------------------------------------------------------------------------
# python
# ---------------------------------------------------------------------------

_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _python(text: str) -> tuple[Declaration, ...]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        # Every one of these means the same thing from here: no structure. See the module
        # docstring for why this is not raised.
        return ()

    lines = _line_starts(text)
    found = [
        _from_node(node, text, lines, prefix="")
        for node in tree.body
        if isinstance(node, _DEFINITIONS)
    ]
    return _cover(text, found)


def _from_node(node: ast.stmt, text: str, lines: list[int], *, prefix: str) -> Declaration:
    """One definition, with its decorators, its signature, and its nested definitions."""
    assert isinstance(node, _DEFINITIONS)
    decorators = [decorator.lineno for decorator in node.decorator_list]
    first = min([node.lineno, *decorators])
    start = lines[first - 1]
    end = _end_of(node, text, lines)

    # The signature runs from the `def`/`class` line to the line before the body opens, so
    # a multi-line parameter list survives into the header intact. A one-liner has its
    # body on the same line, and then the signature is that line up to the colon.
    body_line = node.body[0].lineno if node.body else node.lineno + 1
    header_start = lines[node.lineno - 1]
    boundary = body_line - 1 if body_line > node.lineno else node.lineno
    header_end = lines[boundary] if boundary < len(lines) else len(text)
    header = text[header_start:header_end].strip()

    name = f"{prefix}{node.name}"
    children = tuple(
        _from_node(child, text, lines, prefix=f"{name}.")
        for child in node.body
        if isinstance(child, _DEFINITIONS)
    )
    return Declaration(start=start, end=end, header=header, name=name, children=children)


def _end_of(node: ast.stmt, text: str, lines: list[int]) -> int:
    """The offset just past a node, taking its whole last line.

    Whole lines rather than ``end_col_offset``, so a trailing comment on the closing line
    stays with the function it is about.
    """
    last = node.end_lineno or node.lineno
    return lines[last] if last < len(lines) else len(text)


def _line_starts(text: str) -> list[int]:
    """Offset of each line start, followed by the length. ``lines[n - 1]`` is line ``n``."""
    starts = [0]
    for index, character in enumerate(text):
        if character == "\n":
            starts.append(index + 1)
    if starts[-1] != len(text):
        starts.append(len(text))
    return starts


# ---------------------------------------------------------------------------
# javascript, typescript, go
# ---------------------------------------------------------------------------

_JS_DECLARATION = re.compile(
    r"^(?:export\s+(?:default\s+)?)?(?:"
    r"(?:async\s+)?function\s*\*?\s*(?P<fn>[A-Za-z_$][\w$]*)"
    r"|(?:abstract\s+)?class\s+(?P<cls>[A-Za-z_$][\w$]*)"
    r"|interface\s+(?P<iface>[A-Za-z_$][\w$]*)"
    r"|enum\s+(?P<enum>[A-Za-z_$][\w$]*)"
    r"|type\s+(?P<alias>[A-Za-z_$][\w$]*)\s*="
    r"|(?:const|let|var)\s+(?P<var>[A-Za-z_$][\w$]*)"
    r"\s*(?::\s*[^=\n]+)?=\s*(?:async\s+)?(?:function\b|\(|<|[A-Za-z_$][\w$]*\s*=>)"
    r")"
)

_JS_MEMBER = re.compile(
    r"^\s*(?:(?:static|async|get|set|public|private|protected|readonly|override|abstract)\s+)*"
    r"(?P<name>[A-Za-z_$#][\w$]*)\s*(?:<[^>\n]*>)?\s*\("
)

_GO_DECLARATION = re.compile(
    r"^func\s+(?:\((?P<receiver>[^)]*)\)\s*)?(?P<fn>[A-Za-z_]\w*)"
    r"|^type\s+(?P<type>[A-Za-z_]\w*)\s+(?:struct|interface)\b"
)

#: Keywords that open a brace and are not declarations. Without this an ``if`` at column
#: zero inside a Go ``init`` would be read as the start of a top-level unit.
_NOT_DECLARATIONS = frozenset({"if", "for", "while", "switch", "try", "catch", "do", "else"})


def _braced(text: str, language: str) -> tuple[Declaration, ...]:
    """Declaration lines plus brace matching.

    A file whose braces do not balance is treated as unparseable, which catches both a
    genuine syntax error and a scanner that got lost in a construct it does not know.
    """
    code = _code_mask(text, language)
    if code is None:
        return ()
    lines = _line_starts(text)
    pattern = _GO_DECLARATION if language == "go" else _JS_DECLARATION

    found: list[Declaration] = []
    for number in range(len(lines) - 1):
        start = lines[number]
        if found and start < found[-1].end:
            continue  # inside the declaration already taken
        line = text[start : lines[number + 1]]
        if line[:1].isspace() or not line.strip():
            continue  # only column zero is a top-level declaration in any of the three
        match = pattern.match(line)
        if match is None or match.group(0).split()[0] in _NOT_DECLARATIONS:
            continue
        if len(line.rstrip()) > MAX_DECLARATION_LINE:
            # Minified. Not "skip this one": a file with one line like this has no line
            # structure at all, and the units found on the rest of it would be arbitrary.
            return ()
        end, header_end = _extent(text, code, start)
        if end is None:
            return ()
        name = _name_of(match, language)
        children = (
            _members(text, code, lines, header_end, end, prefix=f"{name}.")
            if name and match.groupdict().get("cls")
            else ()
        )
        found.append(
            Declaration(
                start=start,
                end=end,
                header=text[start:header_end].strip(),
                name=name,
                children=children,
            )
        )
    return _cover(text, found)


def _name_of(match: re.Match[str], language: str) -> str | None:
    groups = match.groupdict()
    if language == "go":
        receiver = (groups.get("receiver") or "").split()
        owner = receiver[-1].lstrip("*") if receiver else ""
        function = groups.get("fn") or groups.get("type")
        return f"{owner}.{function}" if owner and function else function
    for key in ("fn", "cls", "iface", "enum", "alias", "var"):
        if groups.get(key):
            return groups[key]
    return None


def _extent(text: str, code: list[bool], start: int) -> tuple[int | None, int]:
    """Where a declaration beginning at ``start`` ends, and where its header ends.

    A declaration with a body ends at the matching closing brace. One without — ``type
    Result = string``, an interface written on one line — ends at the end of its
    statement. ``None`` means the braces never balanced, which is this scanner's way of
    saying it does not understand this file.
    """
    depth = 0
    opened = False
    header_end = start
    position = start
    while position < len(text):
        character = text[position]
        if code[position]:
            if character == "{":
                depth += 1
                if not opened:
                    opened = True
                    header_end = position + 1
            elif character == "}":
                depth -= 1
                if opened and depth == 0:
                    return _line_end(text, position), header_end
            elif not opened and character in ";\n" and _statement_ends(text, code, position):
                return _line_end(text, position), position
        position += 1
    return (None, start) if opened else (len(text), _line_end(text, start))


def _statement_ends(text: str, code: list[bool], position: int) -> bool:
    """Whether a newline really ends the statement, rather than continuing it.

    A signature broken across lines leaves an open bracket behind it; ending the unit
    there would cut a declaration in half at its first comma.
    """
    if text[position] == ";":
        return True
    line = text[text.rfind("\n", 0, position) + 1 : position]
    stripped = line.rstrip()
    return not stripped.endswith((",", "(", "[", "=", "|", "&", "+", "\\"))


def _line_end(text: str, position: int) -> int:
    following = text.find("\n", position)
    return len(text) if following < 0 else following + 1


def _members(
    text: str, code: list[bool], lines: list[int], start: int, stop: int, *, prefix: str
) -> tuple[Declaration, ...]:
    """Methods of a class, found one level inside its braces.

    Depth is tracked rather than indentation guessed at, so a method whose body contains
    an object literal does not end the class early and a nested function does not come
    back as a second method.
    """
    found: list[Declaration] = []
    depth = 1
    position = start
    while position < stop:
        if code[position]:
            if text[position] == "{":
                depth += 1
            elif text[position] == "}":
                depth -= 1
                if depth <= 0:
                    break
        if depth == 1 and (position == 0 or text[position - 1] == "\n"):
            line = text[position : _line_end(text, position)]
            match = _JS_MEMBER.match(line)
            if match is not None and match.group("name") not in _NOT_DECLARATIONS:
                end, header_end = _extent(text, code, position)
                if end is not None and end <= stop:
                    found.append(
                        Declaration(
                            start=position,
                            end=end,
                            header=text[position:header_end].strip(),
                            name=f"{prefix}{match.group('name')}",
                        )
                    )
                    position = end
                    continue
        position += 1
    return tuple(found)


def _code_mask(text: str, language: str) -> list[bool] | None:
    """``True`` at every character that is code rather than a string or a comment.

    The whole reason this exists is that a brace inside a string literal is not a brace.
    Without it, one ``"}"`` in a log message would end a function two hundred lines early
    and every chunk after it would be misattributed.

    ``None`` for text with an unterminated string or block comment: something is wrong
    with the file, or with this scanner's idea of it, and either way the structure it
    would report is not to be trusted.
    """
    mask = [True] * len(text)
    state = ""  # "", "//", "/*", "'", '"', "`"
    position = 0
    while position < len(text):
        character = text[position]
        following = text[position + 1] if position + 1 < len(text) else ""
        if state == "":
            if character == "/" and following == "/":
                state, mask[position] = "//", False
            elif character == "/" and following == "*":
                state, mask[position] = "/*", False
            elif character in "'\"`":
                state, mask[position] = character, False
            position += 1
            continue

        mask[position] = False
        if state == "//":
            if character == "\n":
                state, mask[position] = "", True
        elif state == "/*":
            if character == "*" and following == "/":
                mask[position + 1] = False
                position += 1
                state = ""
        elif character == "\\" and not (language == "go" and state == "`"):
            # An escaped quote does not close the literal. Go's backtick strings are raw
            # and have no escapes at all, which is why the language is consulted here.
            mask[min(position + 1, len(text) - 1)] = False
            position += 1
        elif character == state:
            state = ""
        elif character == "\n" and state in "'\"":
            # An unterminated single-line string. Rather than run to the end of the file
            # looking for a closing quote, call the file unparseable.
            return None
        position += 1

    return None if state in ("/*", "'", '"', "`") else mask


# ---------------------------------------------------------------------------
# covering the file
# ---------------------------------------------------------------------------


def _cover(text: str, found: list[Declaration]) -> tuple[Declaration, ...]:
    """Declarations plus filler, tiling the whole file. ``()`` if nothing was found.

    Really tiling: consecutive units meet exactly and the last ends at the end of the file.
    A gap that is only whitespace is absorbed into the unit before it rather than given one
    of its own — an empty unit would be dropped by the splitter anyway, and a cover with
    holes in it is a cover nobody can check.

    Returning ``()`` for a file with no declarations at all is what sends a minified
    bundle, a JSON blob named ``.ts``, and a file of nothing but imports down the
    ``recursive`` path — where they belong, since there is no structure here to use.
    """
    if not found:
        return ()
    ordered = sorted(found, key=lambda declaration: declaration.start)
    covered: list[Declaration] = []
    cursor = 0
    for declaration in ordered:
        if declaration.start < cursor:
            continue  # overlapping, which only a scanner bug produces; keep the first
        if declaration.start > cursor:
            gap = text[cursor : declaration.start]
            if gap.strip() or not covered:
                covered.append(
                    Declaration(start=cursor, end=declaration.start, header="", name=None)
                )
            else:
                covered[-1] = _extended(covered[-1], declaration.start)
        covered.append(declaration)
        cursor = declaration.end
    if cursor < len(text):
        if text[cursor:].strip():
            covered.append(Declaration(start=cursor, end=len(text), header="", name=None))
        else:
            covered[-1] = _extended(covered[-1], len(text))
    return tuple(covered)


def _extended(declaration: Declaration, end: int) -> Declaration:
    """The same unit, reaching further. Frozen, so a copy rather than a mutation."""
    return Declaration(
        start=declaration.start,
        end=end,
        header=declaration.header,
        name=declaration.name,
        children=declaration.children,
    )


__all__ = ["MAX_CODE_CHARS", "MAX_DECLARATION_LINE", "Declaration", "declarations"]
