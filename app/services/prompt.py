"""Prompt assembly (SPEC §7).

One system message, built by layered prepend in a fixed order:

.. code-block:: text

    [1] model.system_context      [2] gateway.system_context
    [3] retrieved documents       [4] end-user memory (task 12)
    [5] the client's own system message(s), verbatim and in order

Four decisions are load-bearing, and each of them is a place this could quietly be wrong.

**Assembly is a pure function.** :func:`assemble` takes the request, the two contexts, the
chunks, the facts and the limits, and returns the messages plus an account of what it did.
No clock, no I/O, no service. That is what makes the golden-file tests meaningful and what
makes the editor's prompt preview *the same code* as the request path rather than a second
implementation that drifts.

**The token budget is over the whole rendered block, boilerplate included.** SPEC §6.3
calls ``doc_max_tokens`` a hard cap on injected document text, and the honest reading of
"hard cap" is the one a customer can verify by counting what arrived at the provider. So
the heading and the citation instruction are inside the budget, and a budget too small to
hold them injects nothing at all rather than an orphan heading. It costs about forty
tokens of the allowance and buys a property that is true as stated.

**Chunks are dropped from the tail, and the tail is the lowest score.** Retrieval returns
them ordered, the block preserves that order, and truncation takes from the end. The
alternative — dropping the longest, or the oldest — optimises the token count and loses
the most relevant excerpt, which is exactly backwards.

**The overflow guard only fires when it knows the window.** ``UpstreamTarget.context_window``
is nullable and ``None`` means *unknown*, not unlimited. A guessed window would start
silently withholding memory from requests a provider would have served, which is a worse
failure than the one the guard exists to prevent — and much harder to notice.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.schemas.openai import ChatMessage
from app.services.tokenizer import Tokenizer, WordTokenizer, count

if TYPE_CHECKING:  # pragma: no cover - import cycle: retrieval imports `as_text` from here
    from app.services.facts import Fact
    from app.services.retrieval import Chunk

SYSTEM_ROLE = "system"

#: SPEC §7, verbatim. The instruction is the product surface, not a detail: telling the
#: model to say when the material does not answer the question is the difference between
#: a grounded assistant and a confident liar, and it is one sentence.
REFERENCE_HEADING = "## Reference material"
REFERENCE_INSTRUCTION = (
    "The following excerpts are retrieved from the organization's knowledge base. Cite "
    "them when relevant. If they do not answer the question, say so rather than "
    "inventing an answer."
)
MEMORY_HEADING = "## What you know about this user"

#: Room left for the model's answer when checking the context window. A prompt that fills
#: the window exactly is one the provider accepts and then has nowhere to write into, so
#: the guard has to reserve something; this is a deliberately modest floor rather than an
#: attempt to predict the completion length, which nobody can do from here.
COMPLETION_RESERVE_TOKENS = 256

#: Why a chunk did not make it into the prompt. Recorded per chunk on the request log, so
#: "the answer ignored the pricing page" has an answer that is not a guess.
DROPPED_BUDGET = "doc_max_tokens"
DROPPED_CONTEXT = "context_window"
#: The same, one layer down. A fact dropped by the memory budget and a chunk dropped by
#: the document budget are two different settings to raise, so they are two codes.
DROPPED_MEMORY_BUDGET = "memory_max_tokens"


@dataclass(frozen=True, slots=True)
class Layer:
    """A rendered layer, with its cost. What the editor's preview draws.

    Carries the token count because the preview's whole job is to show a budget being
    spent, and recomputing it in the browser would need the tokenizer in the browser.
    """

    name: str
    label: str
    text: str
    tokens: int


@dataclass(frozen=True, slots=True)
class Assembled:
    """The outbound messages, and an account of how they came to be.

    The account is not decoration. ``injected`` and ``dropped`` become the request log's
    ``retrieved_chunk_ids``; ``memory_tokens`` becomes the column an organization reads to
    see what retrieval costs them; ``overflowed`` is the warning flag SPEC §7 asks for.
    """

    messages: tuple[ChatMessage, ...]
    layers: tuple[Layer, ...] = ()
    injected: tuple[Chunk, ...] = ()
    dropped: tuple[tuple[Chunk, str], ...] = ()
    #: Layer 4's half of the same account. Kept in its own pair of fields rather than
    #: merged into the two above, because the request log stores the two memories in two
    #: columns and a caller asking "what did retrieval find" is not asking "what does it
    #: know about me".
    injected_facts: tuple[Fact, ...] = ()
    dropped_facts: tuple[tuple[Fact, str], ...] = ()
    #: Tokens contributed by layers 3 and 4 together — the whole rendered blocks, which
    #: is what the provider actually charges for.
    memory_tokens: int = 0
    #: Everything the provider is being asked to read, in the tokenizer's own count:
    #: the client's messages, the system layers and the memory blocks. The limiter's
    #: estimate (SPEC §11) and one half of task 101's calibration, taken from the counts
    #: assembly already made rather than a second pass over the same text.
    prompt_tokens: int = 0
    #: Which tokenizer made every count above, by name. Recorded on the request log so
    #: the calibration can group samples by the unit they were measured in.
    tokenizer: str = ""
    #: The client's own messages left no room, so nothing was injected. SPEC §7's warning
    #: flag: the request still goes upstream, because refusing it would be a worse answer
    #: than answering without documents.
    overflowed: bool = False

    @property
    def system_message(self) -> str | None:
        first = self.messages[0] if self.messages else None
        return as_text(first.content) if first is not None and first.role == SYSTEM_ROLE else None

    def chunk_log(self) -> list[dict[str, Any]]:
        """Every retrieved chunk, injected or not, as the request log stores them.

        Both halves in one list rather than two columns: the question the drawer answers
        is "what did retrieval find, and what happened to it", and splitting that across
        two arrays makes the reader join them by eye. ``injected`` is the discriminator.
        """
        return [
            *(chunk.as_log_entry(injected=True) for chunk in self.injected),
            *(
                chunk.as_log_entry(injected=False, dropped_reason=reason)
                for chunk, reason in self.dropped
            ),
        ]

    def fact_log(self) -> list[dict[str, Any]]:
        """Every recalled fact, injected or not, as ``retrieved_fact_ids`` stores them."""
        return [
            *(fact.as_log_entry(injected=True) for fact in self.injected_facts),
            *(
                fact.as_log_entry(injected=False, dropped_reason=reason)
                for fact, reason in self.dropped_facts
            ),
        ]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render_entry(index: int, chunk: Chunk) -> str:
    """One numbered excerpt, in the shape SPEC §7 prints.

    The number is a citation handle — "as [2] says" is only meaningful if the model can
    see a ``[2]`` — so it is positional within the block and starts at 1.
    """
    if chunk.is_summary:
        # Never `source:`. A summary is rewritten text the document does not contain, and
        # a heading that called it a source would invite a citation to words that were
        # never written — the one way task 102 could damage the product.
        return f"[{index}] summary of: {chunk.source_name}\n{chunk.text.strip()}"
    where = f" ({chunk.page_or_section})" if chunk.page_or_section else ""
    return f"[{index}] source: {chunk.source_name}{where}\n{chunk.text.strip()}"


def render_documents(chunks: Sequence[Chunk]) -> str:
    """The whole reference block, or the empty string.

    Empty in, empty out — never a heading with nothing under it. An orphan heading is
    worse than no block at all: it tells the model there is reference material and then
    shows it none, which is a good way to be told about excerpts that do not exist.
    """
    if not chunks:
        return ""
    entries = [render_entry(index, chunk) for index, chunk in enumerate(chunks, start=1)]
    # Heading and instruction on consecutive lines, then a blank line before the first
    # excerpt — the exact shape SPEC §7 prints. The instruction is part of the heading,
    # not the first excerpt, and a blank line between them would read as one.
    header = f"{REFERENCE_HEADING}\n{REFERENCE_INSTRUCTION}"
    return "\n\n".join([header, *entries])


def render_facts(facts: Sequence[Fact]) -> str:
    """Layer 4, in the shape SPEC §7 prints: a heading and one bullet per fact.

    Empty in, empty out — never a heading with nothing under it, for the same reason the
    document block is never rendered empty: telling a model it knows things about this
    user and then listing none is an invitation to invent some.

    Only the fact's *text* is rendered. Not its kind, not its confidence, not the id that
    selected it, and above all not the end user's ``external_id`` — this block is data the
    model reads, it originated in an end user's own conversation, and the less of our
    internal vocabulary appears inside it, the less there is for a crafted fact to
    imitate.

    Each bullet is flattened to one line. A fact containing a newline could otherwise
    close the list visually and start what looks like a new section of the system message,
    which is the cheapest injection there is against a bulleted block and one line of code
    to remove.
    """
    lines = [f"- {flat}" for fact in facts if (flat := _one_line(fact.text))]
    if not lines:
        return ""
    return "\n".join([MEMORY_HEADING, *lines])


def _one_line(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# budgeting
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Budgeted:
    kept: tuple[Chunk, ...]
    dropped: tuple[Chunk, ...]
    text: str
    tokens: int


@dataclass(frozen=True, slots=True)
class BudgetedFacts:
    kept: tuple[Fact, ...]
    dropped: tuple[Fact, ...]
    text: str
    tokens: int


def fit_documents(chunks: Sequence[Chunk], *, budget: int, tokenizer: Tokenizer) -> Budgeted:
    """The longest prefix of ``chunks`` whose rendered block fits in ``budget`` tokens.

    A prefix rather than a subset: dropping chunk 2 and keeping chunk 3 because 3 is
    shorter would reorder relevance by length, and the numbering the model cites would
    stop matching the ranking anyone sees in the editor.

    Re-rendering the block for each candidate length is O(n²) in the number of chunks,
    where n is ``doc_top_k`` — at most 100 by schema and 6 by default. It is also the only
    way to be exactly right: the block's token count is not the sum of its parts, because
    the separators between entries tokenize differently depending on what surrounds them.
    Being exactly right is the acceptance criterion.
    """
    if not chunks or budget <= 0:
        return Budgeted(kept=(), dropped=tuple(chunks), text="", tokens=0)

    best = 0
    best_text = ""
    best_tokens = 0
    for size in range(1, len(chunks) + 1):
        text = render_documents(chunks[:size])
        tokens = count(tokenizer, text)
        if tokens > budget:
            break
        best, best_text, best_tokens = size, text, tokens

    return Budgeted(
        kept=tuple(chunks[:best]),
        dropped=tuple(chunks[best:]),
        text=best_text,
        tokens=best_tokens,
    )


def fit_facts(facts: Sequence[Fact], *, budget: int, tokenizer: Tokenizer) -> BudgetedFacts:
    """The longest prefix of ``facts`` whose rendered block fits in ``budget`` tokens.

    A prefix, exactly as :func:`fit_documents` is, and for a sharper reason. The order it
    preserves puts the always-include facts first — see :mod:`app.services.facts` — so
    truncating from the tail is what makes "these apply to every turn" true under a tight
    budget rather than only when there happens to be room. Dropping the longest fact, or
    the least similar one, would spend the budget better and quietly lose the constraint
    that changes the answer.

    The whole block is measured, heading included, so ``memory_max_tokens`` means what a
    customer can verify by counting what arrived at the provider — the same reading of
    "hard cap" that :func:`fit_documents` uses.
    """
    if not facts or budget <= 0:
        return BudgetedFacts(kept=(), dropped=tuple(facts), text="", tokens=0)

    best = 0
    best_text = ""
    best_tokens = 0
    for size in range(1, len(facts) + 1):
        text = render_facts(facts[:size])
        tokens = count(tokenizer, text)
        if tokens > budget:
            break
        best, best_text, best_tokens = size, text, tokens

    return BudgetedFacts(
        kept=tuple(facts[:best]),
        dropped=tuple(facts[best:]),
        text=best_text,
        tokens=best_tokens,
    )


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def assemble(
    messages: Sequence[ChatMessage],
    *,
    model_context: str | None = None,
    gateway_context: str | None = None,
    chunks: Sequence[Chunk] = (),
    facts: Sequence[Fact] = (),
    doc_max_tokens: int = 0,
    memory_max_tokens: int = 0,
    context_window: int | None = None,
    tokenizer: Tokenizer | None = None,
) -> Assembled:
    """SPEC §7, end to end. Pure: same inputs, same output, no I/O.

    The order of operations matters and is the order SPEC §7 states. The client's own
    messages are measured *first*, because the overflow guard is a question about them —
    "is there room left for anything?" — and the answer decides the document budget. Then
    the document block is truncated to fit, then the memory block, then everything is
    concatenated.
    """
    tokenizer = tokenizer or WordTokenizer()
    conversation = [message for message in messages if message.role != SYSTEM_ROLE]
    client_system = [
        text
        for message in messages
        if message.role == SYSTEM_ROLE and (text := _clean(as_text(message.content)))
    ]

    model_layer = _layer("model.system_context", "Model", _clean(model_context), tokenizer)
    gateway_layer = _layer("gateway.system_context", "Gateway", _clean(gateway_context), tokenizer)
    client_layer = _layer("client.system", "Client", "\n\n".join(client_system), tokenizer)

    # Everything that is going to be sent regardless of what memory adds. This is what
    # "the client's own messages already fill it" is measured against.
    base_tokens = (
        model_layer.tokens
        + gateway_layer.tokens
        + client_layer.tokens
        + sum(count(tokenizer, as_text(message.content)) for message in conversation)
    )

    room = _room_for_memory(base_tokens, context_window)
    budget = min(doc_max_tokens, room)
    # Which constraint bound, for the per-chunk drop reason. `room` binding means the
    # client filled the window; `doc_max_tokens` binding means the gateway's own cap did.
    # An operator reading "dropped: context_window" goes and looks at a different thing
    # from one reading "dropped: doc_max_tokens", so the two are not merged.
    reason = DROPPED_CONTEXT if room < doc_max_tokens else DROPPED_BUDGET

    documents = fit_documents(chunks, budget=budget, tokenizer=tokenizer)

    # SPEC §7: the document block is truncated first, then the memory block, so what is
    # left of the window after documents is what memory may use. The order is the SPEC's
    # and it is the right way round — a document is retrieved for *this* question and is
    # useless once the conversation moves on, while a fact about the person asking is
    # true for every turn, so the block worth keeping when the window is tight is the
    # second one. Documents going first means memory is what survives.
    memory_room = max(0, min(memory_max_tokens, room - documents.tokens))
    memory = fit_facts(facts, budget=memory_room, tokenizer=tokenizer)
    # Which constraint bound, per fact, exactly as the document block records it: an
    # operator reading "dropped: context_window" goes and looks at the client's own
    # messages, and one reading "dropped: memory_max_tokens" goes and raises a number.
    memory_reason = (
        DROPPED_CONTEXT if room - documents.tokens < memory_max_tokens else DROPPED_MEMORY_BUDGET
    )

    rendered = (
        model_layer,
        gateway_layer,
        Layer("documents", "Documents", documents.text, documents.tokens),
        Layer("memory", "Memory", memory.text, memory.tokens),
        client_layer,
    )
    overflowed = room == 0 and bool(chunks or facts)

    parts = [layer.text for layer in rendered if layer.text]
    # Nothing to prepend at all: hand back the client's own list rather than rebuilding
    # it, so a plain pass-through request is byte-identical to what was sent.
    #
    # The *accounting* is returned either way. A request with no system context whose
    # every chunk was dropped is exactly the one somebody opens the drawer for, and an
    # early return that forgot `dropped` would leave its log row silent about it.
    outbound = (
        tuple(messages)
        if not parts
        else (ChatMessage(role=SYSTEM_ROLE, content="\n\n".join(parts)), *conversation)
    )
    return Assembled(
        messages=outbound,
        layers=rendered,
        injected=documents.kept,
        dropped=tuple((chunk, reason) for chunk in documents.dropped),
        injected_facts=memory.kept,
        dropped_facts=tuple((fact, memory_reason) for fact in memory.dropped),
        memory_tokens=documents.tokens + memory.tokens,
        prompt_tokens=base_tokens + documents.tokens + memory.tokens,
        tokenizer=tokenizer.name,
        overflowed=overflowed,
    )


def prompt_tokens(messages: Sequence[ChatMessage], *, tokenizer: Tokenizer) -> int:
    """What the provider is being asked to read, in tokens.

    Over the *assembled* messages, so the retrieved documents and the recalled facts are
    counted along with what the client sent — which is the whole distinction SPEC §11
    draws when it says token limits include injected memory. Structured content is
    flattened by :func:`as_text` first, the same way the budgeting inside
    :func:`assemble` flattens it.
    """
    return sum(count(tokenizer, as_text(message.content)) for message in messages)


def _layer(name: str, label: str, text: str, tokenizer: Tokenizer) -> Layer:
    return Layer(name, label, text, count(tokenizer, text) if text else 0)


def _room_for_memory(base_tokens: int, context_window: int | None) -> int:
    """How many tokens memory may spend, given what is already committed.

    ``None`` — an unknown window — means no ceiling from here, and the gateway's own
    ``doc_max_tokens`` remains the only limit. See the module docstring for why a default
    would be worse than nothing.
    """
    if context_window is None:
        return _UNBOUNDED
    return max(0, context_window - base_tokens - COMPLETION_RESERVE_TOKENS)


#: Stands in for "no context-window ceiling". A number rather than ``None`` so the
#: arithmetic downstream has no special case; far above any real ``doc_max_tokens``, which
#: the schema caps at 100 000.
_UNBOUNDED = 1_000_000_000


def as_text(content: str | list[dict[str, object]] | None) -> str:
    """Flatten message content to plain text.

    Multi-part content (the array form used for images) contributes only its text parts;
    an image in a system message is not something the layering can merge.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = [
        str(part.get("text", ""))
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part)


def _clean(text: str | None) -> str:
    return text.strip() if text else ""


__all__ = [
    "COMPLETION_RESERVE_TOKENS",
    "DROPPED_BUDGET",
    "DROPPED_CONTEXT",
    "DROPPED_MEMORY_BUDGET",
    "MEMORY_HEADING",
    "REFERENCE_HEADING",
    "REFERENCE_INSTRUCTION",
    "Assembled",
    "Budgeted",
    "BudgetedFacts",
    "Layer",
    "as_text",
    "assemble",
    "fit_documents",
    "fit_facts",
    "prompt_tokens",
    "render_documents",
    "render_entry",
    "render_facts",
]
