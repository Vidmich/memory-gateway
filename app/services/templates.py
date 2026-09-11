"""The text a gateway writes around what it injects and around what comes back (task 105).

SPEC §7 prints one shape: ``## Reference material``, an instruction, ``[2] source:
handbook.pdf (p. 12)`` over each excerpt, ``## What you know about this user`` over one
bullet per fact; §7.1 prints ``Sources:`` and ``[2] handbook.pdf (p. 12)`` under an answer.
Every one of those strings was a constant in Python, which is to say a decision made once,
in English, for every customer. This module makes them nine templates on the gateway — the
same shape by default, byte for byte — and renders them.

Three rules, each closing a door that a smaller design would have left open.

**Substitution is the only operation.** :func:`render` replaces ``{name}`` with a value and
does nothing else: no attribute access, no indexing, no format specs, no expressions. It is
a regular expression and a dictionary, never :meth:`str.format` — ``{handle.__class__}``
under ``str.format`` is an evaluation, and the values being substituted are chunk text and
end-user facts, which is exactly the text one must never evaluate anything over.

**A substituted value is never rescanned.** The scan runs once over the *template*; what
it inserts is copied in verbatim. A document that contains ``{text}`` renders those six
characters, and a fact that contains ``{{`` renders both braces.

**The vocabulary is closed, per template.** Each template has a fixed list of names it may
use, and a name outside the list is refused at save time with the list in the message.
An unknown name is far more likely a typo than an intention, and a placeholder that
silently rendered as nothing would be found by reading transcripts.

Two invariants live with the vocabulary and are checked in the same place, because the
rest of the system depends on them: the excerpt template keeps ``[{handle}]``, so the model
can cite and :mod:`app.services.citations` can resolve what it cites; and the fact template
is one line, so :func:`~app.services.prompt.render_facts` flattening each fact still means
what its docstring says it means.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - the schema imports this module for validation
    from app.schemas.gateway_config import TemplateConfig

# ---------------------------------------------------------------------------
# the defaults: SPEC §7 and §7.1, verbatim
# ---------------------------------------------------------------------------

#: The instruction is the product surface, not a detail: telling the model to say when
#: the material does not answer the question is the difference between a grounded
#: assistant and a confident liar, and it is one sentence.
DEFAULT_REFERENCE_HEADING = "## Reference material"
DEFAULT_REFERENCE_INSTRUCTION = (
    "The following excerpts are retrieved from the organization's knowledge base. Cite "
    "them when relevant. If they do not answer the question, say so rather than "
    "inventing an answer."
)
DEFAULT_EXCERPT = "[{handle}] source: {source_name}{section}\n{text}"
DEFAULT_MEMORY_HEADING = "## What you know about this user"
DEFAULT_FACT = "- {text}"
DEFAULT_SOURCES_HEADING = "Sources:"
DEFAULT_SOURCE_LINE = "[{handle}] {label}"
DEFAULT_ANSWER_PREFIX = ""
DEFAULT_ANSWER_SUFFIX = ""

#: The four that take no placeholders. Rendered verbatim, braces and all.
PLAIN: tuple[str, ...] = (
    "reference_heading",
    "reference_instruction",
    "memory_heading",
    "sources_heading",
)

#: The five that take placeholders, and exactly which. Closed: a name not listed here is
#: refused, not rendered empty.
VOCABULARY: Mapping[str, tuple[str, ...]] = {
    "excerpt": ("handle", "source_name", "section", "section_raw", "text", "score"),
    "fact": ("text",),
    "source_line": ("handle", "label", "source_name", "section", "url"),
    "answer_prefix": ("cited_count", "injected_count", "gateway", "model"),
    "answer_suffix": ("cited_count", "injected_count", "gateway", "model"),
}

#: Every template, in the order the page lists them: the request side top to bottom,
#: then the response side.
NAMES: tuple[str, ...] = (
    "reference_heading",
    "reference_instruction",
    "excerpt",
    "memory_heading",
    "fact",
    "sources_heading",
    "source_line",
    "answer_prefix",
    "answer_suffix",
)

#: Prompts are billed per token and a template is multiplied by ``doc_top_k`` on every
#: request, so each has a ceiling. The instruction's is higher because it is a paragraph
#: by design and appears once.
MAX_TEMPLATE_LENGTH = 500
MAX_INSTRUCTION_LENGTH = 2000

# ---------------------------------------------------------------------------
# the grammar
# ---------------------------------------------------------------------------

#: One pass, left to right. ``{{`` and ``}}`` are literal braces; ``{name}`` is a
#: placeholder; anything else — a lone brace, ``{not a name}``, ``{a.b}`` — is left as the
#: characters it is. The scan is over the template only; see the module docstring.
_TOKEN = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: What validation looks for: any brace group at all, so ``{handle.__class__}`` is an
#: unknown placeholder rather than a literal. ``{{``/``}}`` are removed first.
_GROUP = re.compile(r"\{([^{}]*)\}")


def render(template: str, values: Mapping[str, Any]) -> str:
    """``template`` with each ``{name}`` replaced by ``values[name]``, once.

    A name the mapping does not have renders as itself. That cannot happen for a template
    the schema accepted — the vocabulary is checked at save time — and on the request path
    the alternative to rendering the four characters is raising, which is the wrong trade
    for a heading.
    """

    def substitute(match: re.Match[str]) -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        name = match.group(1)
        if name in values:
            return str(values[name])
        return token

    return _TOKEN.sub(substitute, template)


def placeholders(template: str) -> list[str]:
    """Every brace group in ``template``, escaped braces removed, in order of appearance.

    The content of the group, not only well-formed names: this is what validation reads,
    and ``handle.__class__`` has to come back so it can be refused by name.
    """
    stripped = template.replace("{{", "").replace("}}", "")
    return [match.group(1) for match in _GROUP.finditer(stripped)]


def unbalanced(template: str) -> bool:
    """Whether a brace is left over once escapes and groups are accounted for."""
    stripped = _GROUP.sub("", template.replace("{{", "").replace("}}", ""))
    return "{" in stripped or "}" in stripped


def check(name: str, template: str) -> None:
    """Refuse a template the vocabulary or an invariant does not allow.

    Raises :class:`ValueError` with the message the form shows — which placeholder, and
    which are allowed — so the API and the page cannot disagree about why.
    """
    if name in PLAIN:
        return
    allowed = VOCABULARY[name]
    if unbalanced(template):
        raise ValueError("a brace on its own is not allowed; write a literal brace as {{ or }}")
    for found in placeholders(template):
        if found not in allowed:
            raise ValueError(
                f"'{{{found}}}' is not a placeholder of this template. "
                f"Allowed: {', '.join('{' + item + '}' for item in allowed)}."
            )
    if name == "excerpt" and "[{handle}]" not in template:
        raise ValueError(
            "the excerpt template must contain [{handle}]; without it the model cannot "
            "cite and citations cannot resolve"
        )
    if name == "source_line" and "{handle}" not in template:
        raise ValueError(
            "the source line must contain {handle}: the footer keeps the number the model "
            "wrote, and a line without it cannot be matched to the answer"
        )
    if name == "fact":
        if "{text}" not in template:
            raise ValueError("the fact template must contain {text}")
        if "\n" in template or "\r" in template:
            raise ValueError(
                "the fact template must be one line: each fact is flattened to one so a "
                "fact cannot open a new section of the prompt, and a template with a "
                "newline would undo that"
            )


# ---------------------------------------------------------------------------
# the set a gateway renders with
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Templates:
    """The nine strings, resolved, as the assembler and the citation stage take them.

    A frozen dataclass rather than the schema object so :mod:`app.services.prompt` — a
    pure module with no configuration imports — can take one as a default argument, and
    so the golden suite is unchanged by construction: every existing call renders with
    :data:`DEFAULT_TEMPLATES`, which is today's constants.
    """

    reference_heading: str = DEFAULT_REFERENCE_HEADING
    reference_instruction: str = DEFAULT_REFERENCE_INSTRUCTION
    excerpt: str = DEFAULT_EXCERPT
    memory_heading: str = DEFAULT_MEMORY_HEADING
    fact: str = DEFAULT_FACT
    sources_heading: str = DEFAULT_SOURCES_HEADING
    source_line: str = DEFAULT_SOURCE_LINE
    answer_prefix: str = DEFAULT_ANSWER_PREFIX
    answer_suffix: str = DEFAULT_ANSWER_SUFFIX

    @classmethod
    def of(cls, config: TemplateConfig) -> Templates:
        return cls(**{name: getattr(config, name) for name in NAMES})

    @classmethod
    def load(cls, stored: Mapping[str, Any] | None) -> Templates:
        """From a stored or cached blob, permissively: a key that is not a template is
        ignored and a missing one is the default — the same rule every config blob
        loads under, applied without going through the schema."""
        values = {
            name: str(value)
            for name, value in (stored or {}).items()
            if name in NAMES and isinstance(value, str)
        }
        return cls(**values)

    @property
    def wraps(self) -> bool:
        """Whether the response stage has anything to add around the answer. ``False``
        by default, and when false the stage adds no frame at all."""
        return bool(self.answer_prefix or self.answer_suffix)

    @property
    def fingerprint(self) -> str:
        """Sixteen hex characters over the nine strings, in a fixed order.

        Stable for equal templates across processes and builds — it is written to every
        request log row and compared across days — so it is a hash of the JSON of the
        values rather than of the dataclass's repr, which a renamed field would move.
        """
        encoded = json.dumps([getattr(self, name) for name in NAMES], ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict[str, str]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


DEFAULT_TEMPLATES = Templates()


def section_of(page_or_section: str | None) -> str:
    """``{section}``: the parenthesised form the default prints, or nothing.

    Offered ready-made so a template author does not have to express "if there is a
    section" in a language that has no conditionals.
    """
    return f" ({page_or_section})" if page_or_section else ""


__all__ = [
    "DEFAULT_TEMPLATES",
    "MAX_INSTRUCTION_LENGTH",
    "MAX_TEMPLATE_LENGTH",
    "NAMES",
    "PLAIN",
    "VOCABULARY",
    "Templates",
    "check",
    "placeholders",
    "render",
    "section_of",
    "unbalanced",
]
