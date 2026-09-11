"""Rebuilding every collection under a new embedding model, without a gap in retrieval.

SPEC §9.4's procedure, in order: create the next collection, re-embed into it, verify,
swap the alias, and leave the old one for the sweeper. What makes it worth writing down is
the set of things that had to be decided along the way.

**The setting changes at the swap, not before it.** The obvious design is "PATCH the
embedding model, then reindex", and it is wrong: between the write and the swap every new
ingestion would embed with the new model and upsert into the old collection, which has the
old width, and Qdrant would refuse every one of them. So the operator's confirmation
*starts a run*, the run carries the model it is moving to, and the platform setting is
written as the last step. Until then everything — serving, ingestion, distillation — keeps
using the model that matches the live collection, which is the only self-consistent state
available.

**The text is re-embedded from the index, not from the files.** Every chunk's text is in
its Qdrant payload, because retrieval has to return something a prompt can carry. That
makes a reindex a read of the old collection rather than a re-extraction of a corpus of
PDFs — orders of magnitude cheaper, and it is why the estimate can count what it will cost
instead of guessing. A document whose points are missing entirely is not re-extracted here
either; it is reported, and **Resync** on its connector is the path that exists for that.

**Verification is a count and a search, and it happens before the swap.** The count catches
a copy that stopped early; the search catches a collection that is full of vectors nobody
can rank — a dimension that was accepted and a distance metric that was not what retrieval
expects. Both are cheap, both fail closed, and failing leaves the old collection serving,
which is the entire point of building beside it.

**One kind of connector cannot be re-embedded and has to be recut.** Task 20's
``semantic`` strategy decides its boundaries by embedding the document's sentences, so for
a connector on it the *chunks themselves* are a product of the old model. Re-embedding
their stored text would faithfully reproduce the old model's opinion about where the
topics change — the full cost of a reindex, buying nothing. Those connectors go back to
object storage instead: extracted again, cut again with the new model deciding the
boundaries, and written into the same target collection beside the copied points. It is
strictly more expensive, so the estimate counts them separately and says so; and a
deployment whose reindexer has no ingestion pipeline (the control-plane process builds one
to estimate, the worker to run) simply reports them and copies nothing differently.

**Points written during the copy are caught up, not raced.** Ingestion carries on while a
reindex runs, so the source grows underneath the scroll. After the first pass the copy runs
again from the beginning until the two counts agree or a bounded number of attempts is
spent. Upserts are keyed by deterministic point ids, so a repeated page costs time and
changes nothing. The residual — a point written between the final count and the swap — is
one document's chunks, and it is recovered by the next ingestion or resync of that
document rather than pretended away.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from app.core.errors import Conflict, NotFound
from app.core.tenancy import Actor
from app.schemas.connector_config import ChunkingConfig, depends_on_embedding_model
from app.schemas.platform import EmbeddingChoice, PlatformSettingsPatch, ReindexEstimate
from app.services.embeddings import Embedder
from app.services.index_fingerprint import with_embedding_model
from app.services.maintenance_store import ConnectorChunking, MaintenanceStore
from app.services.platform_settings import PlatformSettingsService
from app.services.reindex_store import (
    EMBEDDING,
    FAILED,
    ORGANIZATION,
    PLATFORM,
    SUCCEEDED,
    SWAPPED,
    VERIFYING,
    ReindexStore,
    RunView,
    TargetView,
)
from app.services.summarization import KIND_SOURCE, embedding_input
from app.services.vector_backends import VectorBackends
from app.services.vector_index import COPY_BATCH, successor
from app.services.vector_store import ChunkPoint

logger = logging.getLogger(__name__)

#: Characters per token, for the cost estimate. A rule of thumb rather than a tokenizer
#: run over the whole corpus: the number exists so an operator can tell "a few dollars"
#: from "a few thousand", and running a BPE over a million chunks to refine it would cost
#: more than the answer is worth.
CHARS_PER_TOKEN = 4

#: Chunks sampled to estimate mean length. One page, because the estimate's precision is
#: bounded by CHARS_PER_TOKEN anyway.
ESTIMATE_SAMPLE = 256

#: Catch-up passes after the first copy. Bounded so a collection being written to faster
#: than it can be copied fails visibly instead of looping forever.
MAX_CATCH_UP = 3

#: Pause between pages, so a reindex cannot crowd the embedding provider's rate limit or
#: the serving path's share of the vector store.
PAGE_PAUSE_SECONDS = 0.05

#: How close the counts have to be before the swap. Exact: an off-by-one here is a chunk
#: that will never be retrieved again, and there is no reason to accept one.
COUNT_TOLERANCE = 0


class Recutter(Protocol):
    """Re-extract and re-chunk one document under a given model, writing nothing.

    A port with one implementation — :meth:`app.services.ingestion.IngestionPipeline.recut`
    — and it is a port rather than a direct dependency for a reason with teeth: this module
    is built in the API process to *estimate* a reindex, and the API process has no reason
    to hold an extraction subprocess pool. ``None`` there means the estimate still counts
    what a recut would cost and the run, which happens in the worker, is the only thing
    that needs to be able to do one.
    """

    async def recut(
        self, *, organization_id: uuid.UUID, document_id: uuid.UUID, embedder: Embedder
    ) -> list[ChunkPoint]: ...


class RunTracker(Protocol):
    """The per-connector reprocessing runs a platform reindex leaves behind (task 104).

    A port so that this module, which the API process builds to *estimate*, does not
    depend on the reprocessing service; the worker wires the real one. ``None`` means the
    recut is untracked, which is what it was before task 104.
    """

    async def open_for_reindex(
        self,
        *,
        organization_id: uuid.UUID,
        connector_id: uuid.UUID,
        reindex_run_id: uuid.UUID,
        documents: Sequence[uuid.UUID],
    ) -> Any: ...

    async def settle_recut(
        self,
        *,
        organization_id: uuid.UUID,
        run_id: uuid.UUID,
        document_id: uuid.UUID,
        outcome: str,
        tokens: int = 0,
        chunk_count: int | None = None,
    ) -> None: ...

    async def adopt_embedding_model(
        self, organization_id: uuid.UUID, embedding_model: str
    ) -> None: ...


class MissingRecutter(RuntimeError):
    """A run reached a connector that has to be recut and has nothing to recut with.

    Loud, and it fails the target rather than falling through to a plain copy. A silent
    fallback would re-embed a semantic connector's old boundaries, report success, and
    leave a collection whose chunk boundaries came from a model that is no longer in use —
    with nothing anywhere saying so.
    """


class ReindexInProgress(Conflict):
    """A run already covers this scope. Deliberately a 409 rather than a queue: two
    reindexes of one collection would fight over the alias, and the operator's next
    question is always "what is already running", which the message answers."""


@dataclass(frozen=True, slots=True)
class Progress:
    """A run's completion and how long the rest is likely to take."""

    done: int
    total: int
    eta_seconds: int | None

    @property
    def fraction(self) -> float:
        return self.done / self.total if self.total else 1.0


def progress_of(run: RunView, *, now: datetime | None = None) -> Progress:
    """Points done over points expected, plus an ETA at the observed rate.

    ``None`` for the ETA until at least one point has been copied. A number extrapolated
    from nothing is worse than a blank on a screen somebody is using to decide whether to
    wait — and the first seconds of a run are exactly when it would be most wrong.
    """
    total = sum(target.total_points for target in run.targets)
    done = sum(target.done_points for target in run.targets)
    moment = now or datetime.now(UTC)
    elapsed = (moment - run.started_at).total_seconds()
    if done <= 0 or elapsed <= 0 or done >= total:
        return Progress(done=done, total=total, eta_seconds=None)
    rate = done / elapsed
    return Progress(done=done, total=total, eta_seconds=int((total - done) / rate))


class Reindexer:
    """Estimates, starts and runs reindexes. One object, three phases.

    The estimate and the start are control-plane work — a person is waiting — and the run
    is a job. They live together because the estimate has to count exactly what the run
    will copy, and two implementations of "what is in scope" is how an operator ends up
    confirming a cost for one set of collections and paying for another.
    """

    def __init__(
        self,
        store: ReindexStore,
        *,
        backends: VectorBackends,
        maintenance: MaintenanceStore,
        settings: PlatformSettingsService,
        embedder_for: Callable[[EmbeddingChoice], Embedder],
        recutter: Recutter | None = None,
        batch_size: int = COPY_BATCH,
        pause_seconds: float = PAGE_PAUSE_SECONDS,
        tracker: RunTracker | None = None,
    ) -> None:
        self._store = store
        self._backends = backends
        self._maintenance = maintenance
        self._settings = settings
        self._embedder_for = embedder_for
        self._recutter = recutter
        self._batch = batch_size
        self._pause = pause_seconds
        self._tracker = tracker

    # -- estimating ------------------------------------------------------

    async def estimate(
        self,
        *,
        choice: EmbeddingChoice | None = None,
        organization_id: uuid.UUID | None = None,
    ) -> ReindexEstimate:
        current = await self._settings.current()
        target = choice or current.embedding
        organizations = await self._scope(organization_id)

        collections: list[str] = []
        points = 0
        characters = 0
        sampled = 0
        for identifier in organizations:
            index = await self._backends.admin_for(identifier)
            live = await index.live_collection(identifier)
            if live is None:
                continue
            collections.append(live)
            held = await index.count_points(live)
            points += held
            if held and sampled < ESTIMATE_SAMPLE:
                page = await index.scroll(live, cursor=None, limit=ESTIMATE_SAMPLE)
                for point in page.points:
                    characters += len(str(point.payload.get("text", "")))
                    sampled += 1

        recut = await self._recut_scope(organization_id)
        mean = characters / sampled if sampled else 0.0
        return ReindexEstimate(
            collections=sorted(collections),
            organizations=len(collections),
            points=points,
            tokens=int(points * mean / CHARS_PER_TOKEN),
            from_model=current.embedding.name,
            to_model=target.name,
            to_dimension=target.dimension,
            recut_connectors=len(recut),
            recut_documents=sum(row.indexed_documents for row in recut.values()),
        )

    async def _recut_scope(
        self, organization_id: uuid.UUID | None
    ) -> dict[uuid.UUID, ConnectorChunking]:
        """Connectors whose chunk boundaries came out of the embedding model.

        Read from the connector rows rather than from the chunk payloads, because the
        question is what the *next* ingestion would do — a connector switched to
        ``semantic`` yesterday and not yet reindexed still has to be recut, and its stored
        chunks say nothing about that.
        """
        async with self._maintenance.begin() as transaction:
            rows = await transaction.connector_chunkings()
        return {
            row.connector_id: row
            for row in rows
            if (organization_id is None or row.organization_id == organization_id)
            and depends_on_embedding_model(ChunkingConfig.load(row.chunking))
        }

    async def _scope(self, organization_id: uuid.UUID | None) -> list[uuid.UUID]:
        if organization_id is not None:
            return [organization_id]
        async with self._maintenance.begin() as transaction:
            return [identifier for identifier, _name in await transaction.organizations()]

    # -- reading ---------------------------------------------------------

    async def find(self, run_id: uuid.UUID) -> RunView | None:
        async with self._store.begin() as transaction:
            return await transaction.run(run_id)

    async def recent(self, *, limit: int = 10) -> list[RunView]:
        async with self._store.begin() as transaction:
            return await transaction.recent(limit=limit)

    async def running(self) -> RunView | None:
        """The run in flight, if there is one.

        Public because two screens ask: Settings, to say that the embedding model it is
        showing is not the one being moved to, and Maintenance, to draw the progress bar.
        """
        async with self._store.begin() as transaction:
            found = await transaction.running()
        return found[0] if found else None

    # -- starting --------------------------------------------------------

    async def start(
        self,
        actor: Actor | None,
        *,
        choice: EmbeddingChoice | None = None,
        organization_id: uuid.UUID | None = None,
    ) -> RunView:
        """Record the decision and lay out the work. Copies nothing.

        Targets are created here rather than in the run so that a run which never starts —
        the worker is down, the queue is backed up — still shows an operator what it is
        going to do and against which collections.
        """
        current = await self._settings.current()
        target = choice or current.embedding

        async with self._store.begin() as transaction:
            for existing in await transaction.running():
                if existing.covers(organization_id):
                    raise ReindexInProgress(
                        "A reindex is already running for this scope. "
                        f"Started {existing.started_at:%Y-%m-%d %H:%M} UTC.",
                    )

            estimate = await self.estimate(choice=target, organization_id=organization_id)
            scope = ORGANIZATION if organization_id is not None else PLATFORM
            run = await transaction.create_run(
                scope=scope,
                organization_id=organization_id,
                from_model=current.embedding.name,
                from_dimension=current.embedding.dimension,
                to_model=target.name,
                to_dimension=target.dimension,
                estimated_points=estimate.points,
                estimated_tokens=estimate.tokens,
                started_by=actor.user_id if actor is not None else None,
            )
            for identifier in await self._scope(organization_id):
                index = await self._backends.admin_for(identifier)
                live = await index.live_collection(identifier)
                if live is None:
                    # Nothing indexed for this tenant. Skipped rather than given an empty
                    # target: a row reading "0 of 0, swapped" on the progress screen is
                    # noise, and the alias will be created by their first upload.
                    continue
                await transaction.add_target(
                    run.id,
                    organization_id=identifier,
                    collection=successor(identifier, live),
                    total=await index.count_points(live),
                )
            await transaction.commit()
            started = await transaction.run(run.id)

        logger.info(
            "reindex started",
            extra={
                "run_id": str(run.id),
                "scope": scope,
                "to_model": target.name,
                "to_dimension": target.dimension,
                "estimated_points": estimate.points,
            },
        )
        assert started is not None
        return started

    # -- running ---------------------------------------------------------

    async def run(self, run_id: uuid.UUID) -> RunView:
        """Do the work. Safe to call again after a crash: every target resumes."""
        async with self._store.begin() as transaction:
            run = await transaction.run(run_id)
        if run is None:
            raise NotFound("No such reindex run.")

        # The provider comes from the live configuration and the model from the run. The
        # run records the model and the width, because those are what a collection has to
        # agree with; the provider is a routing detail that can change under an unchanged
        # model, and pinning it would make a reindex fail after a base-URL move.
        current = await self._settings.current()
        embedder = self._embedder_for(
            EmbeddingChoice(
                provider=current.embedding.provider,
                name=run.to_model,
                dimension=run.to_dimension,
            )
        )
        failures: list[str] = []
        for target in run.targets:
            if target.status == SWAPPED:
                continue
            try:
                await self._rebuild(target, embedder=embedder, dimension=run.to_dimension)
            except Exception as error:
                failures.append(f"{target.organization_id}: {error}")
                logger.exception(
                    "reindex target failed; the old collection is still serving",
                    extra={"run_id": str(run_id), "organization_id": str(target.organization_id)},
                )
                async with self._store.begin() as transaction:
                    await transaction.save_target(target.id, status=FAILED, error=str(error)[:500])
                    await transaction.commit()

        async with self._store.begin() as transaction:
            await transaction.finish_run(
                run_id,
                status=FAILED if failures else SUCCEEDED,
                error="; ".join(failures)[:1000] if failures else None,
            )
            await transaction.commit()

        if not failures:
            # The last step, and the reason the setting was not written earlier: every
            # collection now has the new width, so the model that produces query vectors
            # can finally change to match.
            await self._adopt(run)

        async with self._store.begin() as transaction:
            finished = await transaction.run(run_id)
        assert finished is not None
        return finished

    async def _rebuild(self, target: TargetView, *, embedder: Embedder, dimension: int) -> None:
        organization_id = target.organization_id
        index = await self._backends.admin_for(organization_id)
        live = await index.live_collection(organization_id)
        if live is None or live == target.collection:
            # Either nothing to copy, or a resumed run whose swap already happened. Both
            # are "already done" rather than an error — which is what makes calling this
            # twice safe.
            async with self._store.begin() as transaction:
                await transaction.save_target(target.id, status=SWAPPED)
                await transaction.commit()
            return

        await index.create_collection(target.collection, dimension=dimension)
        async with self._store.begin() as transaction:
            await transaction.save_target(target.id, status=EMBEDDING)
            await transaction.commit()

        recut = await self._recut_scope(organization_id)
        if recut and self._recutter is None:
            raise MissingRecutter(
                f"{len(recut)} connector(s) here chunk with the embedding model and have to "
                "be recut, and this process was built without an ingestion pipeline"
            )
        # The recut runs first, so its points are in place before the count that decides
        # whether the copy caught up — otherwise the first catch-up pass would always fire.
        produced = await self._recut(target, embedder=embedder, connectors=set(recut))
        skipped = await self._copy(target, source=live, embedder=embedder, skip=set(recut))

        for _ in range(MAX_CATCH_UP):
            expected = await index.count_points(live) - skipped + produced
            if await index.count_points(target.collection) >= expected - COUNT_TOLERANCE:
                break
            # Points arrived while the scroll was running. Copying from the beginning
            # again is correct and cheap enough: the upserts are keyed by deterministic
            # ids, so everything already there is overwritten with the same value.
            async with self._store.begin() as transaction:
                await transaction.save_target(target.id, clear_cursor=True)
                await transaction.commit()
            skipped = await self._copy(
                replace(target, cursor=None), source=live, embedder=embedder, skip=set(recut)
            )

        # The count, not the running total: a catch-up pass rewrites points the first pass
        # already wrote, and a sum of what was *sent* would report more than exists.
        done = await index.count_points(target.collection)
        async with self._store.begin() as transaction:
            await transaction.save_target(target.id, status=VERIFYING, done_points=done)
            await transaction.commit()
        await self._verify(
            target,
            expected=await index.count_points(live) - skipped + produced,
            embedder=embedder,
        )

        await index.promote(organization_id, target.collection)
        async with self._store.begin() as transaction:
            await transaction.save_target(target.id, status=SWAPPED, done_points=done)
            await transaction.commit()
        logger.info(
            "reindex swapped an alias",
            extra={
                "organization_id": str(organization_id),
                "from": live,
                "to": target.collection,
                "points": done,
            },
        )

    async def _copy(
        self,
        target: TargetView,
        *,
        source: str,
        embedder: Embedder,
        skip: set[uuid.UUID] | None = None,
    ) -> int:
        """Scroll the source, embed each page, upsert it, save the cursor. Resumable.

        Returns how many points it *left behind* — the ones belonging to a connector being
        recut. That number, not the number copied, is what the count check downstream
        needs: the recut writes its own points, and there is no reason for the two to agree
        in count, because deciding the boundaries differently is the entire point of it.

        The cursor is saved *after* the upsert lands, so a crash costs one repeated page
        rather than a silently skipped one — the same ordering, and the same reasoning, as
        ``distilled_at`` being set after the facts are written.
        """
        index = await self._backends.admin_for(target.organization_id)
        cursor = target.cursor
        done = target.done_points
        left = 0
        while True:
            page = await index.scroll(source, cursor=cursor, limit=self._batch)
            wanted = [point for point in page.points if not _belongs_to(point, skip)]
            left += len(page.points) - len(wanted)
            if wanted:
                await index.upsert_into(target.collection, await _embed(wanted, embedder))
                done += len(wanted)
                async with self._store.begin() as transaction:
                    await transaction.save_target(target.id, done_points=done, cursor=page.cursor)
                    await transaction.commit()
            cursor = page.cursor
            if cursor is None:
                return left
            if self._pause:
                await asyncio.sleep(self._pause)

    async def _recut(
        self, target: TargetView, *, embedder: Embedder, connectors: set[uuid.UUID]
    ) -> int:
        """Rebuild each recut connector's documents from object storage into the target.

        A document that will not read or will not extract is **skipped and counted**, not
        raised on. It is one file of a corpus, the old collection still holds whatever it
        had for it, and failing a whole tenant's reindex because somebody deleted an object
        would be a far worse trade. What it costs stays honest: the number returned is what
        was actually produced, so the verification compares against reality rather than
        against an intention.
        """
        if not connectors or self._recutter is None:
            return 0
        index = await self._backends.admin_for(target.organization_id)
        produced = 0
        missed = 0
        for connector_id in sorted(connectors):
            async with self._maintenance.begin() as transaction:
                documents = await transaction.indexed_documents(connector_id)
            # Task 104: the connector's own screen shows this operation's progress for
            # its documents, as a reprocessing run with `trigger: embedding_model`.
            run_id: uuid.UUID | None = None
            if self._tracker is not None:
                run = await self._tracker.open_for_reindex(
                    organization_id=target.organization_id,
                    connector_id=connector_id,
                    reindex_run_id=target.run_id,
                    documents=documents,
                )
                run_id = run.id
            for document_id in documents:
                try:
                    points = await self._recutter.recut(
                        organization_id=target.organization_id,
                        document_id=document_id,
                        embedder=embedder,
                    )
                except Exception:
                    missed += 1
                    logger.warning(
                        "a document could not be recut; the rest of the reindex continues",
                        exc_info=True,
                        extra={
                            "organization_id": str(target.organization_id),
                            "document_id": str(document_id),
                        },
                    )
                    await self._settle(target, run_id, document_id, "failed")
                    continue
                if points:
                    await index.upsert_into(target.collection, points)
                    produced += len(points)
                await self._settle(target, run_id, document_id, "done", points)
                if self._pause:
                    await asyncio.sleep(self._pause)
        logger.info(
            "recut connectors rebuilt from object storage",
            extra={
                "organization_id": str(target.organization_id),
                "connectors": len(connectors),
                "points": produced,
                "documents_skipped": missed,
            },
        )
        return produced

    async def _settle(
        self,
        target: TargetView,
        run_id: uuid.UUID | None,
        document_id: uuid.UUID,
        outcome: str,
        points: Sequence[ChunkPoint] = (),
    ) -> None:
        if self._tracker is None or run_id is None:
            return
        await self._tracker.settle_recut(
            organization_id=target.organization_id,
            run_id=run_id,
            document_id=document_id,
            outcome=outcome,
            tokens=sum(int(point.payload.get("token_count", 0) or 0) for point in points),
            chunk_count=(
                sum(1 for point in points if point.payload.get("kind", KIND_SOURCE) == KIND_SOURCE)
                if outcome == "done"
                else None
            ),
        )

    async def _verify(self, target: TargetView, *, expected: int, embedder: Embedder) -> None:
        """Counts, then a search. Raising here leaves the old collection live.

        ``expected`` is passed in rather than read off the source, because with a recut in
        the run the source's own count is no longer the right number: some of its points
        were deliberately not copied, and what replaced them is a different quantity of
        chunks by design.
        """
        index = await self._backends.admin_for(target.organization_id)
        actual = await index.count_points(target.collection)
        if actual < expected - COUNT_TOLERANCE:
            raise RuntimeError(
                f"{target.collection} holds {actual} points and {expected} were expected"
            )
        if not expected:
            return
        width = await index.collection_dimension(target.collection)
        if width != embedder.dimension:
            raise RuntimeError(
                f"{target.collection} was built with width {width}, "
                f"the model produces {embedder.dimension}"
            )
        page = await index.scroll(target.collection, cursor=None, limit=1)
        probe = str(page.points[0].payload.get("text", "")) if page.points else ""
        vector = (await embedder.embed([probe or "sample"]))[0]
        found = await index.search_in(target.collection, vector, limit=1)
        if not found:
            # A collection with points that answers no query at all is one nothing can
            # retrieve from. Better to fail here, with the old index still serving, than
            # to swap and discover it from a support ticket.
            raise RuntimeError(f"a sample search against {target.collection} returned nothing")

    async def _adopt(self, run: RunView) -> None:
        """Write the embedding the run just made real.

        Only for a platform-wide run. A single organization reindexed onto a different
        model would leave every other tenant's collection at the old width, and a platform
        setting that describes one tenant is a setting that is wrong for the rest.
        """
        if run.scope != PLATFORM:
            return
        current = await self._settings.current()
        if current.embedding.name != run.to_model:
            from app.services.platform_store import platform_attribution

            await self._settings.update(
                platform_attribution("reindex"),
                PlatformSettingsPatch(
                    embedding={"name": run.to_model, "dimension": run.to_dimension},
                    confirm_reindex=run.to_model,
                ),
            )
        if self._tracker is not None:
            # Task 104. Every row in every swapped tenant now holds vectors from the new
            # model: rewrite the rows' model and fingerprint segment to say so, and
            # recompute their statuses against the model that is now the platform's.
            for target in run.targets:
                await self._tracker.adopt_embedding_model(target.organization_id, run.to_model)


def _belongs_to(point: ChunkPoint, connectors: set[uuid.UUID] | None) -> bool:
    """Whether a stored point came from one of these connectors.

    A payload with no readable ``connector_id`` answers ``False`` and is therefore copied.
    Copying a point that should have been recut leaves one document cut by the old model;
    dropping one that should have been copied loses it outright. The first is recoverable
    with a resync and the second is not.
    """
    if not connectors:
        return False
    raw = point.payload.get("connector_id")
    try:
        return uuid.UUID(str(raw)) in connectors
    except (TypeError, ValueError):
        return False


async def _embed(points: Sequence[ChunkPoint], embedder: Embedder) -> list[ChunkPoint]:
    # What the vector was computed from, not `text`: under `sentence_window` that is the
    # matched sentence and under `contextual` summarization (task 102) it carries the
    # document's summary in front. Re-embedding the bare text would silently drop both.
    texts = [embedding_input(point.payload) for point in points]
    vectors = await embedder.embed(texts)
    return [
        ChunkPoint(id=point.id, vector=list(vector), payload=_re_embedded(point.payload, embedder))
        for point, vector in zip(points, vectors, strict=True)
    ]


def _re_embedded(payload: Mapping[str, Any], embedder: Embedder) -> dict[str, Any]:
    """The payload of a copied point: the same, with its index fingerprint's model
    segment moved to the model that just produced the vector (task 104). A point with no
    structured fingerprint keeps what it has."""
    copied = dict(payload)
    recorded = copied.get("index_fingerprint")
    if recorded:
        copied["index_fingerprint"] = with_embedding_model(str(recorded), embedder.model)
    return copied


def estimated_cost_lines(estimate: ReindexEstimate) -> list[str]:
    """The estimate as sentences, for a confirmation dialog and for a log line."""
    lines = [
        f"{estimate.organizations} collection(s): {', '.join(estimate.collections) or 'none'}",
        f"{estimate.points} chunks, about {estimate.tokens} tokens to embed",
        f"{estimate.from_model or 'unset'} to {estimate.to_model} "
        f"({estimate.to_dimension} dimensions)",
    ]
    if estimate.recut_connectors:
        # A different *kind* of cost, not a bigger one, which is why it gets its own line:
        # it re-reads object storage and extracts again, so it is what explains a run that
        # takes far longer than its chunk count suggests.
        lines.append(
            f"{estimate.recut_connectors} connector(s) chunk with the embedding model and "
            f"will be recut from object storage: {estimate.recut_documents} document(s)"
        )
    return lines


__all__ = [
    "CHARS_PER_TOKEN",
    "MAX_CATCH_UP",
    "MissingRecutter",
    "Progress",
    "Recutter",
    "ReindexInProgress",
    "Reindexer",
    "RunTracker",
    "estimated_cost_lines",
    "progress_of",
]
