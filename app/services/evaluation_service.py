"""The control-plane half of retrieval evaluation (task 103, SPEC §6.6).

Sets, items, runs, and the two ways of getting labels without typing them: importing the
request log, and asking a model. :mod:`app.services.evaluation` is the arithmetic and
:mod:`app.services.evaluation_runner` is the job; what is here is every operation a
screen performs on the tables, with the rules that keep the numbers honest.

**Labels from citations are free and biased; labels from people are expensive and few;
labels from a model are cheap and circular.** The service keeps all three apart in
``source`` and ``verified`` and never blends them: an imported item arrives unverified, a
generated one arrives unverified *and* marked generated, and only a person's confirmation
moves an item into the verified column a run reports separately.

**A label is checked against this organization's index when it is written.** A chunk id
from another organization is a 404, not a hit — the same answer as everywhere else for a
row that exists and is not yours — because the alternative is an evaluation set that
scores a gateway against chunks it can never retrieve, which would look like a retrieval
failure and be a labelling one.

**Generation spends, through task 102's chain and into task 102's ledger,** with
``purpose: evaluation`` so the summarization panel's document counts do not absorb it and
the bill is still one table.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.api.proxy.errors import UpstreamStatus
from app.core.errors import Conflict, NotFound, Validation
from app.core.ids import uuid7
from app.core.tenancy import Actor, TenantScope
from app.db.models import EvaluationItem, EvaluationRun, EvaluationSet, Gateway
from app.db.models.validation import ITEM_SOURCES, MAX_QUESTION_LENGTH, MAX_SET_NAME_LENGTH
from app.schemas.config import merge_config
from app.schemas.gateway_config import MemoryConfig
from app.schemas.openai import ChatMessage, ChatRequest, ChatResponse
from app.services.audit_snapshots import subject, target_of
from app.services.connector_store import ConnectorStore
from app.services.evaluation import (
    MAX_ITEMS_PER_RUN,
    SOURCE_CITATION,
    SOURCE_GENERATED,
    SOURCE_LOG,
    SOURCE_MANUAL,
    Label,
    labels_of,
    normalise_question,
)
from app.services.evaluation_store import (
    QUEUED,
    RUNNING,
    SUCCEEDED,
    EvaluationStore,
    ItemCounts,
)
from app.services.gateway_store import GatewayStore
from app.services.jobs import EVALUATE_SET, JobOutbox, JobQueue, evaluate_key
from app.services.metrics_store import LogFilters, MetricsRepository
from app.services.params import Resolved
from app.services.proxy import Prepared
from app.services.summarization import KIND_SOURCE
from app.services.summarization_store import (
    PURPOSE_EVALUATION,
    RunRecord,
    SummarizationStore,
)
from app.services.summarization_store import SUCCEEDED as LEDGER_SUCCEEDED
from app.services.summarizer import Completer, SummaryModels
from app.services.vector_store import VectorStore

logger = logging.getLogger(__name__)

NO_SUCH_GATEWAY = "No such gateway."
NO_SUCH_SET = "No such evaluation set."
NO_SUCH_ITEM = "No such evaluation item."
NO_SUCH_RUN = "No such evaluation run."
NO_SUCH_CHUNK = "No such chunk."
NO_SUCH_DOCUMENT = "No such document."

#: Log rows read per import. Distinct questions come out the other end; this bounds the
#: scan, not the set.
IMPORT_SCAN_LIMIT = 2000
#: Items generated per call. Each is a model call; a screen presses the button again.
GENERATE_MAX = 25
#: A chunk shorter than this has no question worth writing.
GENERATE_MIN_TOKENS = 40
#: The generated question's ceiling in the model's reply.
QUESTION_MAX_TOKENS = 80
#: How many items a diff names per direction (won / lost).
DIFF_ITEMS = 20

_QUOTES = "\"'`" + "".join(chr(code) for code in (0x201C, 0x201D, 0x2018, 0x2019))


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SetView:
    set: EvaluationSet
    counts: ItemCounts
    last_run: EvaluationRun | None


@dataclass(frozen=True, slots=True)
class SetDetail:
    set: EvaluationSet
    counts: ItemCounts
    items: Sequence[EvaluationItem]


@dataclass(frozen=True, slots=True)
class ItemDraft:
    question: str
    relevant: Sequence[Mapping[str, Any]] = ()
    relevant_document_ids: Sequence[uuid.UUID] = ()
    source: str = SOURCE_MANUAL
    verified: bool = True
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class ImportResult:
    imported: int
    duplicates: int
    #: Rows read that had no importable question — no stored body, or no user turn.
    skipped: int
    #: Of the imported, how many arrived with citation labels.
    labelled: int


@dataclass(frozen=True, slots=True)
class GenerateResult:
    generated: int
    tokens_in: int
    tokens_out: int
    model_name: str | None
    #: What the model refused or mangled, so the count and the bill can disagree honestly.
    failed: int


@dataclass(frozen=True, slots=True)
class MetricDelta:
    name: str
    before: float | None
    after: float | None

    @property
    def change(self) -> float | None:
        if self.before is None or self.after is None:
            return None
        return self.after - self.before


@dataclass(frozen=True, slots=True)
class RunDiff:
    """Two runs of one set, and what changed between them."""

    before: EvaluationRun
    after: EvaluationRun
    metrics: tuple[MetricDelta, ...]
    #: ``memory_config`` keys whose values differ, with both values.
    config_changes: dict[str, tuple[Any, Any]]
    #: Sentences: the embedding model moved, connector X's fingerprints changed, ...
    index_changes: tuple[str, ...]
    #: Items that were hits after and misses before, and the reverse, at chunk level.
    won: tuple[dict[str, Any], ...]
    lost: tuple[dict[str, Any], ...]


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Written:
    question: str
    model_name: str
    tokens_in: int
    tokens_out: int


class QuestionWriter:
    """Asks a model for the question a chunk answers, and writes the bill.

    The same model chain as summarization — connector, organization default, distillation
    model, platform default — because the organization already chose a cheap model for
    exactly this kind of work, and a fourth setting for it would be a fourth thing to leave
    unset. Recorded in ``summarization_runs`` with ``purpose: evaluation``.
    """

    def __init__(
        self, *, models: SummaryModels, proxy: Completer, ledger: SummarizationStore
    ) -> None:
        self._models = models
        self._proxy = proxy
        self._ledger = ledger

    async def write(
        self,
        *,
        organization_id: uuid.UUID,
        connector_id: uuid.UUID,
        document_id: uuid.UUID,
        source_name: str,
        text: str,
    ) -> Written | None:
        """One question, or ``None`` when the model refused or returned nothing usable.
        Raises :class:`Validation` when no model resolves — before any call is made."""
        target = await self._models.target(organization_id, None)
        if target is None:
            raise Validation(
                "No model is configured to generate questions with: set a summarization or "
                "distillation model for this organization, or a platform default."
            )
        started = time.perf_counter()
        messages = _question_messages(text, source_name)
        estimate_in = sum(len(str(m.content or "")) // 4 for m in messages)
        outcome = LEDGER_SUCCEEDED
        tokens_in, tokens_out, estimated = estimate_in, 0, True
        question: str | None = None
        error: str | None = None
        try:
            response = await self._proxy.complete(
                Prepared(
                    request=ChatRequest.model_validate(
                        {
                            "model": target.upstream_model_id,
                            "messages": [m.model_dump(exclude_none=True) for m in messages],
                            "temperature": 0.3,
                            "max_tokens": QUESTION_MAX_TOKENS,
                        }
                    ),
                    params=Resolved(values={}),
                    target=target,
                )
            )
            question = parse_question(_content_of(response))
            usage = response.usage
            if usage is not None and (usage.prompt_tokens or usage.completion_tokens):
                tokens_in = int(usage.prompt_tokens or 0)
                tokens_out = int(usage.completion_tokens or 0)
                estimated = False
            elif question:
                tokens_out = len(question) // 4
            if question is None:
                outcome, error = "failed", "malformed_question"
        except UpstreamStatus as exc:
            outcome, error = "failed", f"provider_refused:{exc.status_code}"
        scope = TenantScope.of_organization(organization_id)
        async with self._ledger.begin(scope) as transaction:
            await transaction.record(
                RunRecord(
                    organization_id=organization_id,
                    connector_id=connector_id,
                    document_id=document_id,
                    outcome=outcome,
                    reason=error,
                    model_id=target.id,
                    model_name=target.name,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    estimated=estimated,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    purpose=PURPOSE_EVALUATION,
                )
            )
            await transaction.commit()
        if question is None:
            return None
        return Written(
            question=question,
            model_name=target.name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )


def _question_messages(text: str, source_name: str) -> list[ChatMessage]:
    return [
        ChatMessage(
            role="system",
            content=(
                "You write evaluation questions for a document search system. Given a "
                "passage, write the one question a user would ask that this passage "
                "answers. The question must be answerable from the passage alone, must not "
                "mention the passage or the document, and must read like something a real "
                "person would type. Reply with the question only."
            ),
        ),
        ChatMessage(
            role="user",
            content=f"Passage from {source_name}:\n\n{text.strip()}\n\nThe question:",
        ),
    ]


def parse_question(reply: str | None) -> str | None:
    """The first line of the reply, unquoted, ending in a question mark; ``None`` when
    there is nothing question-shaped in it."""
    if not reply:
        return None
    line = next((part.strip() for part in reply.strip().splitlines() if part.strip()), "")
    line = re.sub(r"^(?:question|q)\s*[:\-]\s*", "", line, flags=re.IGNORECASE)
    line = line.strip(_QUOTES + " ").strip()
    if not line or len(line) < 8:
        return None
    if not line.endswith("?"):
        line = line.rstrip(".!") + "?"
    return line[:MAX_QUESTION_LENGTH]


def _content_of(response: ChatResponse) -> str | None:
    for choice in response.choices:
        if choice.message is not None and choice.message.content:
            return str(choice.message.content)
    return None


# ---------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------


class EvaluationService:
    def __init__(
        self,
        store: EvaluationStore,
        *,
        gateways: GatewayStore,
        connectors: ConnectorStore,
        vectors: VectorStore,
        logs: MetricsRepository,
        queue: JobQueue,
        writer: QuestionWriter | None = None,
    ) -> None:
        self._store = store
        self._gateways = gateways
        self._connectors = connectors
        self._vectors = vectors
        self._logs = logs
        self._queue = queue
        self._writer = writer

    # -- sets ---------------------------------------------------------------

    async def sets(self, actor: Actor, gateway_id: uuid.UUID) -> list[SetView]:
        await self._gateway(actor, gateway_id)
        async with self._store.begin(actor.scope) as transaction:
            rows = await transaction.sets(gateway_id)
            counts = await transaction.item_counts([row.id for row in rows])
            views = []
            for row in rows:
                runs = await transaction.runs(row.id, limit=1)
                views.append(
                    SetView(
                        set=row,
                        counts=counts.get(row.id, ItemCounts()),
                        last_run=runs[0] if runs else None,
                    )
                )
        return views

    async def create_set(
        self, actor: Actor, gateway_id: uuid.UUID, *, name: str, description: str | None = None
    ) -> SetView:
        gateway = await self._gateway(actor, gateway_id)
        clean = _clean_name(name)
        row = EvaluationSet(
            id=uuid7(),
            organization_id=gateway.organization_id,
            gateway_id=gateway_id,
            name=clean,
            description=(description or "").strip() or None,
            created_by=actor.user_id,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        async with self._store.begin(actor.scope) as transaction:
            await transaction.add_set(row)
            transaction.audit(actor, "evaluation_set.create", after=subject(row))
            await transaction.commit()
        logger.info(
            "evaluation set created",
            extra={
                "set_id": str(row.id),
                "gateway_id": str(gateway_id),
                "audit_action": "evaluation_set.create",
            },
        )
        return SetView(set=row, counts=ItemCounts(), last_run=None)

    async def detail(self, actor: Actor, set_id: uuid.UUID) -> SetDetail:
        async with self._store.begin(actor.scope) as transaction:
            row = await transaction.set(set_id)
            if row is None:
                raise NotFound(NO_SUCH_SET)
            items = await transaction.items(set_id)
            counts = await transaction.item_counts([set_id])
        return SetDetail(set=row, counts=counts.get(set_id, ItemCounts()), items=items)

    async def update_set(
        self,
        actor: Actor,
        set_id: uuid.UUID,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> SetDetail:
        async with self._store.begin(actor.scope) as transaction:
            row = await transaction.set(set_id)
            if row is None:
                raise NotFound(NO_SUCH_SET)
            before = subject(row)
            if name is not None:
                row.name = _clean_name(name)
            if description is not None:
                row.description = description.strip() or None
            row.updated_at = datetime.now(UTC)
            transaction.audit(actor, "evaluation_set.update", before=before, after=subject(row))
            await transaction.commit()
        return await self.detail(actor, set_id)

    async def delete_set(self, actor: Actor, set_id: uuid.UUID) -> None:
        async with self._store.begin(actor.scope) as transaction:
            row = await transaction.set(set_id)
            if row is None:
                raise NotFound(NO_SUCH_SET)
            transaction.audit(actor, "evaluation_set.delete", before=subject(row))
            await transaction.delete_set(row)
            await transaction.commit()

    # -- items ----------------------------------------------------------------

    async def add_item(self, actor: Actor, set_id: uuid.UUID, draft: ItemDraft) -> EvaluationItem:
        if draft.source not in ITEM_SOURCES:
            raise Validation(f"source is one of {', '.join(ITEM_SOURCES)}.", param="source")
        question = _clean_question(draft.question)
        async with self._store.begin(actor.scope) as transaction:
            evaluation_set = await transaction.set(set_id)
            if evaluation_set is None:
                raise NotFound(NO_SUCH_SET)
        labels = await self._check_labels(
            actor, evaluation_set.organization_id, draft.relevant, draft.relevant_document_ids
        )
        row = EvaluationItem(
            id=uuid7(),
            organization_id=evaluation_set.organization_id,
            set_id=set_id,
            question=question,
            relevant=[label.as_json() for label in labels],
            relevant_document_ids=[str(value) for value in draft.relevant_document_ids],
            source=draft.source,
            verified=draft.verified,
            notes=(draft.notes or "").strip() or None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        async with self._store.begin(actor.scope) as transaction:
            await transaction.add_item(row)
            transaction.audit(actor, "evaluation_item.create", after=subject(row))
            await transaction.commit()
        return row

    async def update_item(
        self, actor: Actor, item_id: uuid.UUID, patch: Mapping[str, Any]
    ) -> EvaluationItem:
        allowed = {"question", "relevant", "relevant_document_ids", "verified", "notes"}
        unknown = sorted(set(patch) - allowed)
        if unknown:
            raise Validation(f"Unknown fields: {', '.join(unknown)}.", param=unknown[0])
        async with self._store.begin(actor.scope) as transaction:
            row = await transaction.item(item_id)
            if row is None:
                raise NotFound(NO_SUCH_ITEM)
            organization_id = row.organization_id
        relevant = None
        document_ids = None
        if "relevant" in patch or "relevant_document_ids" in patch:
            try:
                document_ids = [
                    uuid.UUID(str(value))
                    for value in patch.get("relevant_document_ids", row.relevant_document_ids or [])
                ]
            except ValueError as exc:
                raise Validation(
                    "relevant_document_ids must be UUIDs.", param="relevant_document_ids"
                ) from exc
            labels = await self._check_labels(
                actor, organization_id, patch.get("relevant", row.relevant or []), document_ids
            )
            relevant = [label.as_json() for label in labels]
        async with self._store.begin(actor.scope) as transaction:
            row = await transaction.item(item_id)
            if row is None:
                raise NotFound(NO_SUCH_ITEM)
            before = subject(row)
            if "question" in patch:
                row.question = _clean_question(str(patch["question"]))
            if relevant is not None:
                row.relevant = relevant
            if document_ids is not None:
                row.relevant_document_ids = [str(value) for value in document_ids]
            if "verified" in patch:
                row.verified = bool(patch["verified"])
            if "notes" in patch:
                notes = patch["notes"]
                row.notes = str(notes).strip() or None if notes is not None else None
            row.updated_at = datetime.now(UTC)
            transaction.audit(actor, "evaluation_item.update", before=before, after=subject(row))
            await transaction.commit()
        return row

    async def delete_item(self, actor: Actor, item_id: uuid.UUID) -> None:
        async with self._store.begin(actor.scope) as transaction:
            row = await transaction.item(item_id)
            if row is None:
                raise NotFound(NO_SUCH_ITEM)
            transaction.audit(actor, "evaluation_item.delete", before=subject(row))
            await transaction.delete_item(row)
            await transaction.commit()

    # -- import ---------------------------------------------------------------

    async def import_from_log(
        self,
        actor: Actor,
        set_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
        uncited: bool | None = None,
        limit: int = MAX_ITEMS_PER_RUN,
    ) -> ImportResult:
        """One item per distinct user question the gateway answered in the window.

        Pre-labelled with what the answer cited (task 100) where it cited anything —
        ``source: citation`` — and unlabelled, ``source: log``, where it did not. Both
        arrive unverified. Deduplicated by normalised text against each other and against
        what the set already holds, so importing the same week twice adds nothing.
        """
        if end <= start:
            raise Validation("The window is empty.", param="start")
        limit = max(1, min(limit, MAX_ITEMS_PER_RUN))
        async with self._store.begin(actor.scope) as transaction:
            evaluation_set = await transaction.set(set_id)
            if evaluation_set is None:
                raise NotFound(NO_SUCH_SET)
            existing = {
                normalise_question(item.question) for item in await transaction.items(set_id)
            }
        filters = LogFilters(
            start=start, end=end, gateway_id=evaluation_set.gateway_id, uncited=uncited
        )
        async with self._logs.begin(actor.scope) as logs:
            rows = await logs.retrieval_questions(filters, limit=IMPORT_SCAN_LIMIT)

        drafts: list[EvaluationItem] = []
        duplicates = 0
        skipped = 0
        labelled = 0
        seen = set(existing)
        organization_id = evaluation_set.organization_id
        now = datetime.now(UTC)
        for row in rows:
            key = normalise_question(row.question)
            if not key:
                skipped += 1
                continue
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            labels = await self._labels_from_citations(
                organization_id, row.retrieved, row.cited_chunk_ids
            )
            if labels:
                labelled += 1
            drafts.append(
                EvaluationItem(
                    id=uuid7(),
                    organization_id=organization_id,
                    set_id=set_id,
                    question=row.question[:MAX_QUESTION_LENGTH],
                    relevant=[label.as_json() for label in labels],
                    relevant_document_ids=[],
                    source=SOURCE_CITATION if labels else SOURCE_LOG,
                    verified=False,
                    notes=f"Imported from request {row.log_id} of {row.created_at.date()}.",
                    created_at=now,
                    updated_at=now,
                )
            )
            if len(drafts) >= limit:
                break

        async with self._store.begin(actor.scope) as transaction:
            for draft in drafts:
                await transaction.add_item(draft)
            transaction.audit(
                actor,
                "evaluation_set.import",
                target=target_of(evaluation_set),
                summary={
                    "imported": len(drafts),
                    "labelled": labelled,
                    "duplicates": duplicates,
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                },
            )
            await transaction.commit()
        logger.info(
            "evaluation items imported",
            extra={
                "set_id": str(set_id),
                "imported": len(drafts),
                "labelled": labelled,
                "duplicates": duplicates,
                "audit_action": "evaluation_set.import",
            },
        )
        return ImportResult(
            imported=len(drafts), duplicates=duplicates, skipped=skipped, labelled=labelled
        )

    async def _labels_from_citations(
        self,
        organization_id: uuid.UUID,
        retrieved: Sequence[Mapping[str, Any]],
        cited: Sequence[str],
    ) -> list[Label]:
        """The cited chunks as labels, with their text fetched from the index so the label
        survives a recut. A cited chunk that is already gone keeps its id and document and
        has no text; the run reports it unanchored."""
        wanted = set(cited)
        labels = []
        cache: dict[str, dict[str, str]] = {}
        for entry in retrieved:
            chunk_id = str(entry.get("id", ""))
            if chunk_id not in wanted:
                continue
            document_id = entry.get("document_id")
            text = None
            if document_id:
                key = str(document_id)
                if key not in cache:
                    try:
                        stored = await self._vectors.chunks(organization_id, uuid.UUID(key))
                    except ValueError:
                        stored = []
                    cache[key] = {chunk.id: chunk.text for chunk in stored}
                text = cache[key].get(chunk_id)
            labels.append(
                Label(
                    chunk_id=chunk_id,
                    document_id=str(document_id) if document_id else None,
                    source_name=str(entry.get("source_name") or "") or None,
                    text=text,
                )
            )
        return labels

    # -- generate -------------------------------------------------------------

    async def generate(self, actor: Actor, set_id: uuid.UUID, *, count: int) -> GenerateResult:
        """Write ``count`` questions from a spread of the gateway's indexed chunks.

        Each chunk becomes an item whose label is that chunk, ``source: generated``,
        unverified. The set's screen and every run over it say so: a question written
        *from* the answer finds the answer more easily than a question a person asked.
        """
        if self._writer is None:
            raise Validation("Question generation is not available in this deployment.")
        if not 1 <= count <= GENERATE_MAX:
            raise Validation(f"count is between 1 and {GENERATE_MAX}.", param="count")
        async with self._store.begin(actor.scope) as transaction:
            evaluation_set = await transaction.set(set_id)
            if evaluation_set is None:
                raise NotFound(NO_SUCH_SET)
            existing = {
                label.chunk_id
                for item in await transaction.items(set_id)
                for label in labels_of(item.relevant or [])
            }
        gateway = await self._gateway(actor, evaluation_set.gateway_id)
        config = MemoryConfig.load(gateway.memory_config)
        organization_id = evaluation_set.organization_id
        candidates = await self._candidate_chunks(actor, config, existing, count)
        if not candidates:
            raise Validation(
                "Nothing to generate from: the gateway's connectors have no indexed chunks "
                "that are not already labelled in this set."
            )

        rows: list[EvaluationItem] = []
        tokens_in = tokens_out = failed = 0
        model_name = None
        now = datetime.now(UTC)
        for connector_id, document_id, source_name, chunk_id, text in candidates:
            written = await self._writer.write(
                organization_id=organization_id,
                connector_id=connector_id,
                document_id=document_id,
                source_name=source_name,
                text=text,
            )
            if written is None:
                failed += 1
                continue
            tokens_in += written.tokens_in
            tokens_out += written.tokens_out
            model_name = written.model_name
            rows.append(
                EvaluationItem(
                    id=uuid7(),
                    organization_id=organization_id,
                    set_id=set_id,
                    question=written.question,
                    relevant=[
                        Label(
                            chunk_id=chunk_id,
                            document_id=str(document_id),
                            source_name=source_name,
                            text=text,
                        ).as_json()
                    ],
                    relevant_document_ids=[],
                    source=SOURCE_GENERATED,
                    verified=False,
                    notes=f"Written by {written.model_name} from {source_name}.",
                    created_at=now,
                    updated_at=now,
                )
            )
        async with self._store.begin(actor.scope) as transaction:
            for row in rows:
                await transaction.add_item(row)
            transaction.audit(
                actor,
                "evaluation_set.generate",
                target=target_of(evaluation_set),
                summary={
                    "generated": len(rows),
                    "failed": failed,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "model": model_name,
                },
            )
            await transaction.commit()
        return GenerateResult(
            generated=len(rows),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model_name=model_name,
            failed=failed,
        )

    async def _candidate_chunks(
        self, actor: Actor, config: MemoryConfig, excluded: set[str], count: int
    ) -> list[tuple[uuid.UUID, uuid.UUID, str, str, str]]:
        """``count`` source chunks spread over the gateway's indexed documents: one per
        document, the middle one, so the questions are not all about title pages."""
        organization_id = actor.scope.require_organization()
        documents: list[tuple[uuid.UUID, uuid.UUID, str]] = []
        async with self._connectors.begin(actor.scope) as transaction:
            for connector_id in config.connector_ids:
                rows = await transaction.documents(
                    connector_id, after=None, limit=MAX_ITEMS_PER_RUN, status="indexed"
                )
                documents.extend((connector_id, row.id, row.source_name) for row in rows)
        if not documents:
            return []
        step = max(1, len(documents) // max(count, 1))
        chosen: list[tuple[uuid.UUID, uuid.UUID, str, str, str]] = []
        for offset in range(step):
            for index in range(offset, len(documents), step):
                connector_id, document_id, source_name = documents[index]
                stored = await self._vectors.chunks(organization_id, document_id)
                usable = [
                    chunk
                    for chunk in stored
                    if chunk.payload.get("kind", KIND_SOURCE) == KIND_SOURCE
                    and chunk.id not in excluded
                    and int(chunk.payload.get("token_count", 0) or 0) >= GENERATE_MIN_TOKENS
                ]
                if not usable:
                    continue
                chunk = usable[len(usable) // 2]
                excluded.add(chunk.id)
                chosen.append((connector_id, document_id, source_name, chunk.id, chunk.text))
                if len(chosen) >= count:
                    return chosen
        return chosen

    # -- runs -----------------------------------------------------------------

    async def start_run(
        self, actor: Actor, set_id: uuid.UUID, *, memory_config: Mapping[str, Any] | None = None
    ) -> EvaluationRun:
        """Queue a run. The patch is validated now, through the same merge the save path
        and Try retrieval use, so a form the editor would refuse is refused here with the
        same message rather than failing minutes later in the job."""
        async with self._store.begin(actor.scope) as transaction:
            evaluation_set = await transaction.set(set_id)
            if evaluation_set is None:
                raise NotFound(NO_SUCH_SET)
            counts = await transaction.item_counts([set_id])
            recent = await transaction.runs(set_id, limit=1)
        if not counts.get(set_id, ItemCounts()).total:
            raise Validation("This set has no items to run.")
        if recent and recent[0].status in (QUEUED, RUNNING):
            raise Conflict("A run of this set is already in progress.")
        gateway = await self._gateway(actor, evaluation_set.gateway_id)
        patch = dict(memory_config) if memory_config else None
        merge_config(MemoryConfig, gateway.memory_config, patch, field="memory_config")

        run = EvaluationRun(
            id=uuid7(),
            organization_id=evaluation_set.organization_id,
            set_id=set_id,
            gateway_id=evaluation_set.gateway_id,
            status=QUEUED,
            created_by=actor.user_id,
            patch=patch,
            # Every column that has a database default is set here as well: the memory
            # twin has no INSERT to apply one, and a response built from the row before
            # the flush would otherwise carry ``None`` where the schema says a number.
            config={},
            snapshot={},
            metrics={},
            results=[],
            total_items=min(counts[set_id].total, MAX_ITEMS_PER_RUN),
            completed_items=0,
            created_at=datetime.now(UTC),
        )
        outbox = JobOutbox(self._queue)
        async with self._store.begin(actor.scope) as transaction:
            await transaction.add_run(run)
            transaction.audit(
                actor,
                "evaluation_run.start",
                target=target_of(evaluation_set),
                summary={
                    "run_id": str(run.id),
                    "items": run.total_items,
                    "unsaved_patch": sorted(patch) if patch else [],
                },
            )
            outbox.add(
                EVALUATE_SET,
                {"organization_id": str(run.organization_id), "run_id": str(run.id)},
                idempotency_key=evaluate_key(run.id),
            )
            await transaction.commit()
        await outbox.flush()
        logger.info(
            "evaluation run queued",
            extra={
                "run_id": str(run.id),
                "set_id": str(set_id),
                "audit_action": "evaluation_run.start",
            },
        )
        return run

    async def runs(self, actor: Actor, set_id: uuid.UUID) -> list[EvaluationRun]:
        async with self._store.begin(actor.scope) as transaction:
            if await transaction.set(set_id) is None:
                raise NotFound(NO_SUCH_SET)
            return list(await transaction.runs(set_id))

    async def run(self, actor: Actor, run_id: uuid.UUID) -> EvaluationRun:
        async with self._store.begin(actor.scope) as transaction:
            run = await transaction.run(run_id)
        if run is None:
            raise NotFound(NO_SUCH_RUN)
        return run

    async def diff(self, actor: Actor, run_id: uuid.UUID, against: uuid.UUID) -> RunDiff:
        """What changed between two runs of one set, oldest first."""
        left = await self.run(actor, run_id)
        right = await self.run(actor, against)
        if left.set_id != right.set_id:
            raise Validation("The two runs are of different sets.", param="against")
        if left.status != SUCCEEDED or right.status != SUCCEEDED:
            raise Validation("Both runs have to have finished.", param="against")
        before, after = sorted((left, right), key=lambda run: (run.created_at, run.id))
        return diff_runs(before, after)

    # -- internals --------------------------------------------------------------

    async def _gateway(self, actor: Actor, gateway_id: uuid.UUID) -> Gateway:
        async with self._gateways.begin(actor.scope) as transaction:
            gateway = await transaction.gateway(gateway_id)
        if gateway is None:
            raise NotFound(NO_SUCH_GATEWAY)
        return gateway

    async def _check_labels(
        self,
        actor: Actor,
        organization_id: uuid.UUID,
        relevant: Sequence[Mapping[str, Any]],
        document_ids: Sequence[uuid.UUID],
    ) -> list[Label]:
        """Labels as they will be stored: each chunk looked up in this organization's index
        and its text filled in, each document id checked to be one of ours."""
        labels = labels_of(relevant)
        checked = []
        for label in labels:
            if label.document_id is None:
                raise Validation("A chunk label needs its document_id.", param="relevant")
            try:
                document_id = uuid.UUID(label.document_id)
            except ValueError as exc:
                raise Validation("document_id is not a UUID.", param="relevant") from exc
            stored = {
                chunk.id: (chunk.text, chunk.payload.get("source_name"))
                for chunk in await self._vectors.chunks(organization_id, document_id)
            }
            if label.chunk_id not in stored:
                raise NotFound(NO_SUCH_CHUNK)
            text, name = stored[label.chunk_id]
            checked.append(
                Label(
                    chunk_id=label.chunk_id,
                    document_id=label.document_id,
                    source_name=label.source_name or (str(name) if name else None),
                    text=label.text or text,
                )
            )
        if document_ids:
            async with self._connectors.begin(actor.scope) as transaction:
                for document_id in document_ids:
                    if await transaction.document(document_id) is None:
                        raise NotFound(NO_SUCH_DOCUMENT)
        return checked


# ---------------------------------------------------------------------------
# diffing
# ---------------------------------------------------------------------------

_HEADLINE = (
    ("chunk recall", ("all", "chunk", "recall")),
    ("chunk precision", ("all", "chunk", "precision")),
    ("chunk MRR", ("all", "chunk", "mrr")),
    ("chunk recall after budget", ("all", "chunk_injected", "recall")),
    ("document hit rate", ("all", "document", "hit_rate")),
    ("document hit rate after budget", ("all", "document_injected", "hit_rate")),
    ("verified chunk recall", ("verified", "chunk", "recall")),
    ("verified chunk precision", ("verified", "chunk", "precision")),
)


def diff_runs(before: EvaluationRun, after: EvaluationRun) -> RunDiff:
    metrics = tuple(
        MetricDelta(name=name, before=_dig(before.metrics, path), after=_dig(after.metrics, path))
        for name, path in _HEADLINE
    )
    config_changes = {
        key: (before.config.get(key), after.config.get(key))
        for key in sorted(set(before.config) | set(after.config))
        if before.config.get(key) != after.config.get(key)
    }
    index_changes = list(_index_changes(before.snapshot, after.snapshot))
    if before.total_items != after.total_items:
        index_changes.append(
            f"The set had {before.total_items} items then and {after.total_items} now."
        )
    earlier = {r["item_id"]: r for r in before.results if isinstance(r, Mapping)}
    won: list[dict[str, Any]] = []
    lost: list[dict[str, Any]] = []
    for result in after.results:
        if not isinstance(result, Mapping):
            continue
        previous = earlier.get(result.get("item_id"))
        if previous is None:
            continue
        then = (previous.get("chunk") or {}).get("hit")
        now = (result.get("chunk") or {}).get("hit")
        if then is None or now is None or then == now:
            continue
        entry = {
            "item_id": result.get("item_id"),
            "question": result.get("question"),
            "before_rank": (previous.get("chunk") or {}).get("first_rank"),
            "after_rank": (result.get("chunk") or {}).get("first_rank"),
        }
        (won if now else lost).append(entry)
    return RunDiff(
        before=before,
        after=after,
        metrics=metrics,
        config_changes=config_changes,
        index_changes=tuple(index_changes),
        won=tuple(won[:DIFF_ITEMS]),
        lost=tuple(lost[:DIFF_ITEMS]),
    )


def _index_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    changes = []
    if before.get("embedding_model") != after.get("embedding_model"):
        changes.append(
            f"The embedding model moved from {before.get('embedding_model')} to "
            f"{after.get('embedding_model')}."
        )
    if before.get("tokenizer") != after.get("tokenizer"):
        changes.append(
            f"The budget was counted with {before.get('tokenizer')} then and "
            f"{after.get('tokenizer')} now."
        )
    then = before.get("connectors") or {}
    now = after.get("connectors") or {}
    for connector_id in sorted(set(then) | set(now)):
        name = (now.get(connector_id) or then.get(connector_id) or {}).get("name") or connector_id
        if connector_id not in then:
            changes.append(f"Connector {name} was added to the gateway.")
            continue
        if connector_id not in now:
            changes.append(f"Connector {name} was removed from the gateway.")
            continue
        old = set((then[connector_id].get("fingerprints") or {}).keys())
        new = set((now[connector_id].get("fingerprints") or {}).keys())
        if old != new:
            changes.append(
                f"Connector {name} was reindexed between the runs: its chunk fingerprints "
                f"went from {_names(old)} to {_names(new)}."
            )
    return changes


def _names(values: set[str]) -> str:
    shown = sorted(value or "unrecorded" for value in values)
    return ", ".join(shown) if shown else "none"


def _dig(mapping: Mapping[str, Any], path: tuple[str, ...]) -> float | None:
    value: Any = mapping
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return float(value) if isinstance(value, int | float) else None


def _clean_name(name: str) -> str:
    clean = " ".join(name.split())
    if not clean:
        raise Validation("Give the set a name.", param="name")
    if len(clean) > MAX_SET_NAME_LENGTH:
        raise Validation(f"The name is at most {MAX_SET_NAME_LENGTH} characters.", param="name")
    return clean


def _clean_question(question: str) -> str:
    clean = question.strip()
    if not clean:
        raise Validation("Give the item a question.", param="question")
    return clean[:MAX_QUESTION_LENGTH]


__all__ = [
    "DIFF_ITEMS",
    "GENERATE_MAX",
    "IMPORT_SCAN_LIMIT",
    "EvaluationService",
    "GenerateResult",
    "ImportResult",
    "ItemDraft",
    "MetricDelta",
    "QuestionWriter",
    "RunDiff",
    "SetDetail",
    "SetView",
    "Written",
    "diff_runs",
    "parse_question",
]
