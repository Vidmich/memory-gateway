"""Try retrieval, and see the prompt (SPEC §13.1, Gateways → Memory).

Two read-only questions, asked from the gateway editor:

* **Try retrieval** — for this question, which chunks would be injected, at what score,
  from which document, and which of them survive the token budget?
* **Prompt preview** — for this question, what does the assembled system message actually
  look like, layer by layer, and how much of the model's context window does it use?

The acceptance criterion for the first one is that its answer **exactly matches** what a
real request injects for the same query, and that is a statement about implementation, not
about care: this module calls the same :class:`~app.services.retrieval.MemoryService` the
data plane calls and the same :func:`~app.services.prompt.assemble` it assembles with. A
second implementation that merely agreed today is how a tuning loop becomes a liar.

Two things it deliberately does differently from a request.

**It accepts an unsaved memory configuration.** Tuning ``doc_min_score`` by saving,
sending traffic, and reading the result would change the live endpoint for every caller
between each attempt. The patch is merged through :func:`merge_config`, the same function
the save path uses, so what is previewed is exactly what saving that form would store.

**It reads the gateway row rather than the resolver's cache.** The cache can be up to a
minute stale after a write from another replica; the row cannot. For a diagnostic, the
newer of the two is the right one — and the difference is only ever visible in the seconds
after a save that this screen just made.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.errors import NotFound, Validation
from app.core.tenancy import Actor
from app.db.models import Gateway, UpstreamModel
from app.schemas.config import merge_config
from app.schemas.gateway_config import MemoryConfig
from app.schemas.openai import ChatMessage
from app.services.citations import footer, resolve
from app.services.gateway_store import GatewayStore
from app.services.prompt import Layer, assemble, fit_documents, render_entry
from app.services.retrieval import Chunk, MemoryService, Recall, Retrieval
from app.services.tokenizer import Tokenizer, WordTokenizer, count
from app.services.tokenizers import effective, stored

#: Same answer as everywhere else for "no such gateway" and "belongs to another
#: organization" — see ``tests/test_cross_tenant.py``.
NO_SUCH_GATEWAY = "No such gateway."

MAX_PREVIEW_QUERY = 4000


@dataclass(frozen=True, slots=True)
class PreviewChunk:
    """One retrieved chunk, and what would become of it."""

    chunk: Chunk
    #: Whether it survives ``doc_max_tokens``. The indicator the work item asks for: a
    #: chunk that scored well and was dropped is the single most useful thing this screen
    #: can show, because it means the budget is the constraint rather than the corpus.
    injected: bool
    #: What this chunk costs on its own, rendered as it would appear in the block. Not a
    #: share of the total — the block's separators tokenize differently in context — but
    #: close enough to answer "which of these is eating the budget".
    tokens: int
    #: The ``[n]`` the prompt would number this chunk with (task 100), so a person reading
    #: a logged answer can map ``[3]`` back to a document without opening the drawer.
    #: Positional in retrieval order, exactly as :func:`~app.services.prompt.render_entry`
    #: assigns it — a dropped chunk still shows its number, because that is the number
    #: the model would have seen had the budget been larger.
    handle: int


@dataclass(frozen=True, slots=True)
class RetrievalPreview:
    """What Try retrieval returns."""

    #: The text that was actually embedded, which for ``last_n_turns`` is not the same as
    #: what was typed. Shown, because a surprising result usually has a surprising query.
    query: str
    outcome: str
    latency_ms: int
    error: str | None
    chunks: tuple[PreviewChunk, ...]
    #: Tokens the injected chunks would add, block boilerplate included.
    injected_tokens: int
    doc_max_tokens: int
    #: What the sizes above were measured with — the primary target's tokenizer, so the
    #: screen can say why the same chunk is a different size under a different model
    #: (task 101).
    tokenizer: str = ""


@dataclass(frozen=True, slots=True)
class CitationsPreview:
    """What the client would receive under each citation mode (task 100).

    Built from a *sample* answer that cites the first injected chunks — the editor cannot
    call the model, and does not need to: the shape of the array and the footer is what
    the person choosing a mode wants to see, and it comes from the same
    :func:`~app.services.citations.resolve` and :func:`~app.services.citations.footer` the
    data plane uses, over the same chunks the prompt preview numbered.
    """

    #: The mode the form currently has, so the UI can highlight the example that applies.
    mode: str
    sample_answer: str
    #: The ``citations`` array ``metadata`` would put on the message.
    metadata: tuple[dict[str, Any], ...]
    #: The block ``footer`` would append to the content. Empty when nothing is injected.
    footer: str


@dataclass(frozen=True, slots=True)
class PromptPreview:
    """What Prompt preview returns: the assembled message, and where its tokens went."""

    layers: tuple[Layer, ...]
    system_message: str
    total_tokens: int
    #: ``None`` when the model does not declare one, in which case no percentage can
    #: honestly be drawn — see :mod:`app.services.prompt`.
    context_window: int | None
    model_name: str | None
    overflowed: bool
    retrieval: RetrievalPreview
    citations: CitationsPreview
    tokenizer: str = ""


class MemoryPreview:
    def __init__(
        self,
        store: GatewayStore,
        *,
        memory: MemoryService,
        tokenizer: Tokenizer | None = None,
        ui_base_url: str | None = None,
    ) -> None:
        self._store = store
        self._memory = memory
        #: The fallback for a gateway with no usable target. With one, the preview
        #: measures with that model's tokenizer, exactly as a request would (task 101).
        self._tokenizer = tokenizer or WordTokenizer()
        #: For the links in the citation examples — the same address the data plane puts
        #: on a real citation, so the preview shows what a client would actually get.
        self._ui_base_url = ui_base_url

    async def try_retrieval(
        self,
        actor: Actor,
        gateway_id: uuid.UUID,
        *,
        query: str,
        memory_config: Mapping[str, Any] | None = None,
    ) -> RetrievalPreview:
        text = _check_query(query)
        gateway, config, model = await self._load(actor, gateway_id, memory_config)
        recall = await self._recall(gateway, config, text)
        return self._preview(recall.documents, config, self._tokenizer_for(model))

    async def preview_prompt(
        self,
        actor: Actor,
        gateway_id: uuid.UUID,
        *,
        message: str,
        memory_config: Mapping[str, Any] | None = None,
    ) -> PromptPreview:
        text = _check_query(message)
        gateway, config, model = await self._load(actor, gateway_id, memory_config)
        recall = await self._recall(gateway, config, text)
        tokenizer = self._tokenizer_for(model)

        assembled = assemble(
            [ChatMessage(role="user", content=text)],
            model_context=model.system_context if model is not None else None,
            gateway_context=gateway.system_context,
            chunks=recall.documents.chunks,
            facts=recall.facts,
            doc_max_tokens=config.doc_max_tokens,
            memory_max_tokens=config.memory_max_tokens,
            context_window=model.context_window if model is not None else None,
            tokenizer=tokenizer,
        )
        return PromptPreview(
            layers=assembled.layers,
            system_message=assembled.system_message or "",
            total_tokens=sum(layer.tokens for layer in assembled.layers),
            context_window=model.context_window if model is not None else None,
            model_name=model.name if model is not None else None,
            overflowed=assembled.overflowed,
            retrieval=self._preview(recall.documents, config, tokenizer),
            citations=self._citations(config, assembled.injected),
            tokenizer=tokenizer.name,
        )

    # -- internals --------------------------------------------------------

    async def _load(
        self,
        actor: Actor,
        gateway_id: uuid.UUID,
        patch: Mapping[str, Any] | None,
    ) -> tuple[Gateway, MemoryConfig, UpstreamModel | None]:
        async with self._store.begin(actor.scope) as transaction:
            gateway = await transaction.gateway(gateway_id)
            if gateway is None:
                raise NotFound(NO_SUCH_GATEWAY)
            model = _primary(gateway)
            # Validated through the same merge the save path uses, so a value this screen
            # accepts is a value the form can save — and one it refuses is refused with
            # the same message and the same field path.
            merged = merge_config(MemoryConfig, gateway.memory_config, patch, field="memory_config")
            connectors = await transaction.own_connectors(
                [uuid.UUID(value) for value in merged.get("connector_ids", [])]
            )

        config = MemoryConfig.load(merged)
        # SPEC §5.3 again, and this is the "re-checked at request time" half wearing a
        # different hat: a preview must not be a way to read a connector the gateway may
        # not, and an unsaved patch is exactly where someone would try.
        kept = [value for value in config.connector_ids if value in connectors]
        return gateway, config.model_copy(update={"connector_ids": kept}), model

    async def _recall(self, gateway: Gateway, config: MemoryConfig, text: str) -> Recall:
        return await self._memory.recall(
            organization_id=gateway.organization_id,
            config=config,
            messages=[ChatMessage(role="user", content=text)],
        )

    def _tokenizer_for(self, model: UpstreamModel | None) -> Tokenizer:
        """The primary target's tokenizer, which is what a request through this gateway
        budgets with — derived from the model or overridden on it (task 101)."""
        if model is None:
            return self._tokenizer
        return effective(model.dialect, model.upstream_model_id, stored(model.tokenizer)).tokenizer

    def _preview(
        self, retrieval: Retrieval, config: MemoryConfig, tokenizer: Tokenizer
    ) -> RetrievalPreview:
        budgeted = fit_documents(retrieval.chunks, budget=config.doc_max_tokens, tokenizer=tokenizer)
        survivors = {chunk.id for chunk in budgeted.kept}
        return RetrievalPreview(
            query=retrieval.query,
            outcome=retrieval.outcome,
            latency_ms=retrieval.latency_ms,
            error=retrieval.error,
            chunks=tuple(
                PreviewChunk(
                    chunk=chunk,
                    injected=chunk.id in survivors,
                    tokens=count(tokenizer, render_entry(index, chunk)),
                    handle=index,
                )
                for index, chunk in enumerate(retrieval.chunks, start=1)
            ),
            injected_tokens=budgeted.tokens,
            doc_max_tokens=config.doc_max_tokens,
            tokenizer=tokenizer.name,
        )

    def _citations(self, config: MemoryConfig, injected: Sequence[Chunk]) -> CitationsPreview:
        """One example per mode, for an answer that cites the first two injected chunks.

        Over ``assembled.injected`` and not the retrieval's chunks: only what survived the
        budget has a handle the model can cite, which is the whole point of resolving
        against the assembler's numbering rather than a recount.
        """
        handles = [f"[{index}]" for index in range(1, min(len(injected), 2) + 1)]
        sample = (
            f"According to {' and '.join(handles)}, the answer is …"
            if handles
            else "The documents do not cover this, so the answer would cite nothing."
        )
        resolution = resolve(sample, injected)
        return CitationsPreview(
            mode=config.citations,
            sample_answer=sample,
            metadata=tuple(c.as_json(base_url=self._ui_base_url) for c in resolution.cited),
            footer=footer(resolution.cited, base_url=self._ui_base_url),
        )


def _primary(gateway: Gateway) -> UpstreamModel | None:
    """The target a request is most likely to reach: the first enabled one by priority.

    A preview cannot show a chain without claiming a request goes to all of it, so it
    shows the one that usually answers and says which model that was. For ``failover``
    that is the primary; for ``ab_split`` it is one of two, and the label is what makes
    that visible rather than misleading.
    """
    usable = sorted(
        (target for target in gateway.targets if target.upstream_model.enabled),
        key=lambda target: target.priority,
    )
    return usable[0].upstream_model if usable else None


def _check_query(value: str) -> str:
    text = value.strip()
    if not text:
        raise Validation("Type a question to try.", param="query")
    return text[:MAX_PREVIEW_QUERY]


__all__ = [
    "MAX_PREVIEW_QUERY",
    "MemoryPreview",
    "PreviewChunk",
    "PromptPreview",
    "RetrievalPreview",
]
