"""Running an evaluation (task 103): the job that retrieves for every item and scores it.

The one rule this module exists to keep is the ``memory_preview`` rule, restated: **the
retrieval is the data plane's retrieval.** Each item's question goes through the same
:class:`~app.services.retrieval.MemoryService` a request goes through and the same
:func:`~app.services.memory_preview.resolve_memory_config` Try retrieval resolves the
gateway with, saved configuration or unsaved patch, and the budget is applied with the same
:func:`~app.services.prompt.fit_documents` under the same tokenizer. A second retrieval
implementation that merely agreed today is how a tuning loop becomes a liar, and a test in
``tests/test_evaluation.py`` asserts the two are identical rather than assuming it.

Three things happen around the retrieval.

**Labels are re-anchored.** A chunk id dies with a recut; the label's *text* does not. Before
an item is scored, each chunk label is looked up in the index; one that is gone is found
again by its text among the document's current chunks, and the item is rewritten to point at
the new id (the text stays, so the next recut can do it again). A label whose text is
nowhere is *unanchored*, counted, and scored as a miss — which is honest: the passage is not
in the index, so retrieval cannot find it.

**The state is recorded.** The run row stores the effective configuration, the embedding
model, the tokenizer, and per connector the chunk fingerprints its documents were cut
with, so that two runs can be diffed and the diff says what changed between them.

**Progress is visible.** ``completed_items`` is written every few items; a set of five
hundred questions is minutes, and a screen that shows nothing for minutes is a screen
somebody refreshes into a second run.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from app.core.tenancy import TenantScope
from app.db.models import EvaluationItem, EvaluationRun
from app.schemas.gateway_config import MemoryConfig
from app.schemas.openai import ChatMessage
from app.services.connector_store import ConnectorStore
from app.services.embeddings import Embedder
from app.services.evaluation import (
    MAX_ITEMS_PER_RUN,
    ItemScore,
    Label,
    Retrieved,
    aggregate,
    labels_of,
    reanchor,
    relevant_documents,
    score_item,
)
from app.services.evaluation_store import FAILED, QUEUED, RUNNING, SUCCEEDED, EvaluationStore
from app.services.gateway_store import GatewayStore
from app.services.memory_preview import primary_model, resolve_memory_config, tokenizer_for
from app.services.prompt import fit_documents
from app.services.retrieval import MemoryService, Retrieval
from app.services.tokenizer import Tokenizer, WordTokenizer
from app.services.vector_store import VectorStore

logger = logging.getLogger(__name__)

#: How often ``completed_items`` is written.
PROGRESS_EVERY = 10


class EvaluationRunner:
    def __init__(
        self,
        store: EvaluationStore,
        *,
        gateways: GatewayStore,
        connectors: ConnectorStore,
        memory: MemoryService,
        vectors: VectorStore,
        embedder: Embedder,
        tokenizer: Tokenizer | None = None,
        progress_every: int = PROGRESS_EVERY,
    ) -> None:
        self._store = store
        self._gateways = gateways
        self._connectors = connectors
        self._memory = memory
        self._vectors = vectors
        self._embedder = embedder
        self._tokenizer = tokenizer or WordTokenizer()
        self._every = progress_every

    async def run(self, organization_id: uuid.UUID, run_id: uuid.UUID) -> None:
        scope = TenantScope.of_organization(organization_id)
        async with self._store.begin(scope) as transaction:
            run = await transaction.run(run_id)
            if run is None:
                logger.warning(
                    "evaluation run is gone; nothing to do", extra={"run_id": str(run_id)}
                )
                return
            if run.status not in (QUEUED, RUNNING):
                return
            run.status = RUNNING
            run.started_at = datetime.now(UTC)
            items = list(await transaction.items(run.set_id))[:MAX_ITEMS_PER_RUN]
            run.total_items = len(items)
            set_id, gateway_id, patch = run.set_id, run.gateway_id, run.patch
            await transaction.commit()

        try:
            config, tokenizer, snapshot = await self._prepare(scope, gateway_id, patch)
            await self._update(scope, run_id, config=config, snapshot=snapshot)
            scores = await self._score_all(scope, run_id, organization_id, items, config, tokenizer)
        except Exception as exc:
            logger.warning(
                "evaluation run failed",
                extra={"run_id": str(run_id), "set_id": str(set_id), "error": type(exc).__name__},
                exc_info=True,
            )
            await self._update(scope, run_id, status=FAILED, error=_describe(exc))
            return

        metrics = aggregate(scores, k=config.doc_top_k)
        await self._update(
            scope,
            run_id,
            status=SUCCEEDED,
            metrics=metrics.as_json(),
            results=[score.as_json() for score in scores],
            completed=len(scores),
        )
        logger.info(
            "evaluation run finished",
            extra={
                "run_id": str(run_id),
                "set_id": str(set_id),
                "items": len(scores),
                "recall": metrics.all.chunk.recall,
                "precision": metrics.all.chunk.precision,
                "mrr": metrics.all.chunk.mrr,
            },
        )

    # -- steps --------------------------------------------------------------

    async def _prepare(
        self, scope: Any, gateway_id: uuid.UUID, patch: Mapping[str, Any] | None
    ) -> tuple[MemoryConfig, Tokenizer, dict[str, Any]]:
        async with self._gateways.begin(scope) as transaction:
            gateway = await transaction.gateway(gateway_id)
            if gateway is None:
                raise LookupError("The gateway this set belongs to no longer exists.")
            config = await resolve_memory_config(transaction, gateway, patch)
        tokenizer = tokenizer_for(primary_model(gateway), self._tokenizer)

        connectors: dict[str, Any] = {}
        async with self._connectors.begin(scope) as transaction:
            for connector_id in config.connector_ids:
                row = await transaction.connector(connector_id)
                fingerprints: Counter[str] = Counter()
                for document in await transaction.audit_rows(connector_id):
                    if document.status == "indexed":
                        fingerprints[document.chunk_fingerprint or ""] += 1
                connectors[str(connector_id)] = {
                    "name": row.name if row is not None else None,
                    "fingerprints": dict(fingerprints),
                }
        snapshot = {
            "embedding_model": self._embedder.model,
            "embedding_dimension": self._embedder.dimension,
            "tokenizer": tokenizer.name,
            "connectors": connectors,
        }
        return config, tokenizer, snapshot

    async def _score_all(
        self,
        scope: Any,
        run_id: uuid.UUID,
        organization_id: uuid.UUID,
        items: Sequence[EvaluationItem],
        config: MemoryConfig,
        tokenizer: Tokenizer,
    ) -> list[ItemScore]:
        scores: list[ItemScore] = []
        chunks_of: dict[str, list[tuple[str, str]]] = {}
        for index, item in enumerate(items, start=1):
            labels, reanchored, unanchored = await self._anchor(
                scope, organization_id, item, chunks_of
            )
            retrieval = await self._retrieve(organization_id, config, item.question)
            retrieved = _retrieved(retrieval, config, tokenizer)
            scores.append(
                score_item(
                    item_id=str(item.id),
                    question=item.question,
                    retrieved=retrieved,
                    relevant_chunk_ids=[label.chunk_id for label in labels],
                    relevant_document_ids=relevant_documents(
                        labels, [str(value) for value in item.relevant_document_ids or []]
                    ),
                    k=config.doc_top_k,
                    source=item.source,
                    verified=item.verified,
                    unanchored=unanchored,
                    reanchored=reanchored,
                    outcome=retrieval.outcome,
                    error=retrieval.error,
                )
            )
            if index % self._every == 0:
                await self._update(scope, run_id, completed=index)
        return scores

    async def _retrieve(
        self, organization_id: uuid.UUID, config: MemoryConfig, question: str
    ) -> Retrieval:
        recall = await self._memory.recall(
            organization_id=organization_id,
            config=config,
            messages=[ChatMessage(role="user", content=question)],
        )
        return recall.documents

    async def _anchor(
        self,
        scope: Any,
        organization_id: uuid.UUID,
        item: EvaluationItem,
        chunks_of: dict[str, list[tuple[str, str]]],
    ) -> tuple[list[Label], int, int]:
        """The item's chunk labels as they stand in the index today.

        A label whose chunk is still there is kept. One whose chunk is gone is re-anchored
        by text among the document's current chunks — possibly to two, when the recut split
        the passage — and the item is rewritten so the next run starts from the new ids.
        One that cannot be found is dropped from the scoring and counted.
        """
        labels = labels_of(item.relevant or [])
        if not labels:
            return [], 0, 0
        current: list[Label] = []
        lost: list[Label] = []
        reanchored = 0
        for label in labels:
            if label.document_id is None:
                # Nothing to look the chunk up under. Scored as given.
                current.append(label)
                continue
            if label.document_id not in chunks_of:
                stored = await self._vectors.chunks(organization_id, uuid.UUID(label.document_id))
                chunks_of[label.document_id] = [(chunk.id, chunk.text) for chunk in stored]
            chunks = chunks_of[label.document_id]
            if _still_holds(label, chunks):
                current.append(label)
                continue
            found = reanchor(label.text or "", chunks) if label.text else []
            if not found:
                lost.append(label)
                continue
            reanchored += 1
            current.extend(
                Label(
                    chunk_id=identifier,
                    document_id=label.document_id,
                    source_name=label.source_name,
                    text=label.text,
                )
                for identifier in found
            )
        if reanchored or lost:
            await self._rewrite(scope, item.id, current, lost)
        return current, reanchored, len(lost)

    async def _rewrite(
        self, scope: Any, item_id: uuid.UUID, current: Sequence[Label], lost: Sequence[Label]
    ) -> None:
        """Point the item at the chunks its text lives in now. Lost labels stay on the item
        — the passage may come back with the next upload, and the label is what somebody
        said — but are not what the run scores against."""
        async with self._store.begin(scope) as transaction:
            item = await transaction.item(item_id)
            if item is None:
                return
            item.relevant = [label.as_json() for label in (*current, *lost)]
            await transaction.commit()

    async def _update(
        self,
        scope: Any,
        run_id: uuid.UUID,
        *,
        status: str | None = None,
        config: MemoryConfig | None = None,
        snapshot: Mapping[str, Any] | None = None,
        metrics: Mapping[str, Any] | None = None,
        results: Sequence[Mapping[str, Any]] | None = None,
        completed: int | None = None,
        error: str | None = None,
    ) -> None:
        async with self._store.begin(scope) as transaction:
            run = await transaction.run(run_id)
            if run is None:
                return
            if status is not None:
                run.status = status
                if status in (SUCCEEDED, FAILED):
                    run.finished_at = datetime.now(UTC)
            if config is not None:
                run.config = config.model_dump(mode="json")
            if snapshot is not None:
                run.snapshot = dict(snapshot)
            if metrics is not None:
                run.metrics = dict(metrics)
            if results is not None:
                run.results = [dict(result) for result in results]
            if completed is not None:
                run.completed_items = completed
            if error is not None:
                run.error = error
            await transaction.commit()


def _still_holds(label: Label, chunks: Sequence[tuple[str, str]]) -> bool:
    """Whether the label's chunk is still the label's chunk.

    Point ids are deterministic over ``(document, index)`` (task 09), so a recut gives
    chunk zero of the new cutting the id chunk zero of the old one had — with different
    text under it. An id that exists is therefore not enough; the text the label was made
    from has to still be there. A label with no text is taken at its id's word.
    """
    for identifier, body in chunks:
        if identifier != label.chunk_id:
            continue
        return label.text is None or _fold(label.text) in _fold(body)
    return False


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def _retrieved(retrieval: Retrieval, config: MemoryConfig, tokenizer: Tokenizer) -> list[Retrieved]:
    """What came back, with the budget applied exactly as the prompt would apply it."""
    budgeted = fit_documents(retrieval.chunks, budget=config.doc_max_tokens, tokenizer=tokenizer)
    survivors = {chunk.id for chunk in budgeted.kept}
    return [
        Retrieved(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            score=chunk.score,
            injected=chunk.id in survivors,
            source_name=chunk.source_name,
            page_or_section=chunk.page_or_section,
            chunk_index=chunk.chunk_index,
            text=chunk.text,
        )
        for chunk in retrieval.chunks
    ]


def _describe(error: BaseException) -> str:
    text = str(error).strip()
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def run_is_terminal(run: EvaluationRun) -> bool:
    return run.status in (SUCCEEDED, FAILED)


__all__ = ["PROGRESS_EVERY", "EvaluationRunner", "run_is_terminal"]
