"""Builders for the distillation worker's tests.

Same principle as ``tests/end_user_support.py``: everything is the *second implementation
of a port* rather than a mock, so a test drives the real
:class:`~app.services.distiller.Distiller` against the real
:class:`~app.services.reconciliation.Reconciler`, the real parser and the real
:class:`~app.services.distillation_models.CatalogModelResolver`. Two things are swapped: the
two sockets — PostgreSQL and Qdrant — and the provider.

**The provider is scripted rather than served.** :class:`ScriptedModel` answers
``complete`` with whatever a test queued and keeps the requests it was sent. That is the
seam that makes the interesting half of this feature testable at all: malformed JSON, a
confidence of 95, an unknown kind, a ``supersedes`` naming somebody else's fact, and a
transcript that tries to instruct the extractor are all *replies*, and none of them can be
produced by asking a real model nicely. Keeping the requests is what lets the
prompt-injection tests assert that the conversation arrived fenced and framed as data.

**The transcripts go in through the real write path.** :meth:`DistillationFixture.log`
builds the two rows the log flusher writes, so what a pass reads is shaped exactly like
what production stores — including the detail that ``request_body`` holds the client's
*original* messages, which is the reason a fact cannot be distilled out of a fact that was
already injected.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from prometheus_client import CollectorRegistry

from app.core.crypto import SecretBox
from app.core.ids import uuid7
from app.core.metrics import DistillationMetrics, build_distillation_metrics
from app.core.tenancy import Actor, TenantScope
from app.db.models import EndUser, MemoryFact, Organization, RequestLog, Transcript, UpstreamModel
from app.schemas.distillation import ORG_DISTILLATION
from app.schemas.openai import ChatResponse, Choice, ResponseMessage
from app.services.catalog_store import MemoryCatalogStore
from app.services.debounce import MemoryDebouncer
from app.services.distillation_models import CatalogModelResolver
from app.services.distillation_service import DistillationService
from app.services.distillation_store import MemoryDistillationStore
from app.services.distillation_trigger import DistillationTrigger
from app.services.distiller import Distiller
from app.services.embeddings import HashEmbedder
from app.services.end_user_store import MemoryEndUserStore
from app.services.fact_vectors import MemoryFactVectorStore
from app.services.locks import MemoryLock
from app.services.memory_db import MemoryDatabase
from app.services.proxy import Prepared
from app.services.reconciliation import Reconciler

#: The same width as the other memory fixtures, so a test that wires recall and
#: distillation together does not have to think about two dimensions.
DIMENSION = 64

#: Long enough to clear :data:`app.services.distillation.MIN_USER_CHARS`, so a fixture
#: conversation is one a real pass would bother with.
SAMPLE_USER_TURN = "I work in Rust these days and I would rather have terse answers."


class ModelUnavailable(Exception):
    """What a scripted provider raises when a test breaks it."""


class ScriptedModel:
    """A :class:`~app.services.distiller.Completer` that answers from a queue.

    Replies are consumed in order; the last one repeats once the queue is empty, so a test
    that runs two passes over the same script does not have to say the same thing twice. A
    queued :class:`Exception` is raised instead of returned, which is how the
    failure-isolation tests break the distillation model without breaking anything else.
    """

    def __init__(self, *replies: str | Exception) -> None:
        self.replies: list[str | Exception] = list(replies)
        self.requests: list[Prepared] = []

    def queue(self, *replies: str | Exception) -> None:
        self.replies.extend(replies)

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def last_prompt(self) -> str:
        """The whole text the extractor was sent. What the injection tests read."""
        prepared = self.requests[-1]
        return "\n".join(str(message.content or "") for message in prepared.request.messages)

    async def complete(self, prepared: Prepared) -> ChatResponse:
        self.requests.append(prepared)
        if not self.replies:
            raise AssertionError("the scripted model was called with nothing queued")
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return ChatResponse(
            id="chatcmpl-distil",
            model=prepared.target.upstream_model_id,
            choices=[Choice(index=0, message=ResponseMessage(role="assistant", content=reply))],
        )


class LaggyFactVectors(MemoryFactVectorStore):
    """A fact index whose writes are not visible to search until they settle.

    Real vector stores index asynchronously. The in-memory twin does not, which makes it
    the wrong thing to test *in-pass* deduplication against: a pass that relied on the
    index seeing its own last write would pass here and fail against Qdrant, sometimes.

    So this one holds new points aside until :meth:`settle` is called. A test that
    deduplicates two phrasings within one pass against this store is testing the guard that
    exists for that case rather than the twin's convenient timing.
    """

    def __init__(self) -> None:
        super().__init__()
        self._staged: dict[uuid.UUID, list[Any]] = {}

    async def upsert(self, organization_id: uuid.UUID, points: Sequence[Any]) -> None:
        self._staged.setdefault(organization_id, []).extend(points)

    async def settle(self) -> None:
        for organization_id, points in self._staged.items():
            await super().upsert(organization_id, points)
        self._staged.clear()


def facts_json(*facts: dict[str, Any]) -> str:
    """A well-formed extractor reply. Defaults filled in so a test names only what it means."""
    return json.dumps(
        {
            "facts": [
                {
                    "kind": "preference",
                    "confidence": 0.9,
                    "supersedes": [],
                    "ttl_days": None,
                    **fact,
                }
                for fact in facts
            ]
        }
    )


@dataclass
class DistillationFixture:
    """The whole write half of conversation memory, over memory."""

    database: MemoryDatabase
    organization: Organization
    store: MemoryDistillationStore
    end_users: MemoryEndUserStore
    vectors: MemoryFactVectorStore
    embedder: HashEmbedder
    debouncer: MemoryDebouncer
    lock: MemoryLock
    model: ScriptedModel
    #: The catalog row the resolver will find, when the fixture made one. ``None`` for a
    #: fixture built with ``with_model=False`` — which is what the shared world uses, so
    #: that wiring distillation into every harness does not put an extra model in every
    #: test's catalog listing.
    upstream: UpstreamModel | None
    reconciler: Reconciler
    resolver: CatalogModelResolver
    distiller: Distiller
    trigger: DistillationTrigger
    service: DistillationService
    metrics: DistillationMetrics
    registry: CollectorRegistry
    user_id: uuid.UUID = field(default_factory=uuid7)

    # -- identities -------------------------------------------------------

    @property
    def organization_id(self) -> uuid.UUID:
        return self.organization.id

    @property
    def scope(self) -> TenantScope:
        return TenantScope.of_organization(self.organization_id)

    @property
    def actor(self) -> Actor:
        return Actor(
            user_id=self.user_id,
            scope=TenantScope(role="org_admin", organization_id=self.organization_id),
        )

    async def end_user(self, external_id: str = "alice") -> EndUser:
        async with self.end_users.begin(self.scope) as transaction:
            row = await transaction.touch(external_id)
            await transaction.commit()
            return row

    def configure(self, **values: Any) -> None:
        """Set distillation settings the way the Settings screen would."""
        stored = dict(self.organization.settings or {})
        stored[ORG_DISTILLATION] = {**stored.get(ORG_DISTILLATION, {}), **values}
        self.organization.settings = stored
        self.trigger.forget(self.organization_id)

    # -- transcripts ------------------------------------------------------

    def log(
        self,
        end_user: EndUser,
        *,
        session_id: str | None = "thread-1",
        user_text: str = SAMPLE_USER_TURN,
        assistant_text: str = "Understood.",
        messages: Sequence[dict[str, Any]] | None = None,
        status_code: int = 200,
        created_at: datetime | None = None,
        distilled: bool = False,
    ) -> RequestLog:
        """One logged request and its transcript, shaped the way the flusher writes them."""
        when = created_at or datetime.now(UTC)
        log = RequestLog(
            id=uuid7(),
            created_at=when,
            organization_id=self.organization_id,
            gateway_id=uuid7(),
            end_user_id=end_user.id,
            session_id=session_id,
            status_code=status_code,
            latency_total_ms=100,
            retrieved_chunk_ids=[],
            retrieved_fact_ids=[],
            failover_attempts=[],
        )
        self.database.request_logs[log.id] = log
        self.database.transcripts[log.id] = Transcript(
            request_log_id=log.id,
            created_at=when,
            organization_id=self.organization_id,
            request_body=list(messages)
            if messages is not None
            else [{"role": "user", "content": user_text}],
            assembled_prompt=None,
            response_body=assistant_text,
            distilled_at=when if distilled else None,
        )
        return log

    # -- running ----------------------------------------------------------

    async def distil(
        self, end_user: EndUser, *, session_id: str | None = "thread-1", token: str | None = None
    ) -> Any:
        return await self.distiller.run(
            organization_id=self.organization_id,
            end_user_id=end_user.id,
            session_id=session_id,
            token=token,
        )

    # -- reading back -----------------------------------------------------

    async def facts_of(self, end_user: EndUser) -> list[MemoryFact]:
        async with self.end_users.begin(self.scope) as transaction:
            rows = await transaction.all_facts(end_user.id)
        return sorted(rows, key=lambda row: row.id)

    async def texts_of(self, end_user: EndUser, *, live_only: bool = True) -> list[str]:
        now = datetime.now(UTC)
        return sorted(
            fact.text
            for fact in await self.facts_of(end_user)
            if not live_only or (fact.superseded_at is None and (fact.expires_at or now) >= now)
        )

    async def vector_count(self, end_user: EndUser | None = None) -> int:
        return await self.vectors.count(
            self.organization_id, end_user_id=end_user.id if end_user else None
        )

    def runs(self) -> list[Any]:
        return sorted(self.database.distillation_runs.values(), key=lambda row: row.created_at)

    def transcript(self, log: RequestLog) -> Transcript:
        return self.database.transcripts[log.id]


def build_distillation(
    organization: Organization | None = None,
    *,
    database: MemoryDatabase | None = None,
    model: ScriptedModel | None = None,
    end_users: MemoryEndUserStore | None = None,
    vectors: MemoryFactVectorStore | None = None,
    embedder: HashEmbedder | None = None,
    platform_default: bool = False,
    with_model: bool = True,
    **settings: Any,
) -> DistillationFixture:
    """The worker's objects, over memory, with a scripted provider.

    ``platform_default`` puts the model in the *global* catalog and leaves the
    organization's own selection empty, which is the fallback path SPEC §6.4 describes.

    ``with_model=False`` builds the same objects with nothing in the catalog, which is what
    the shared world uses: adding a distillation model to every fixture would put an extra
    row in every test's model listing, and a test about the catalog should not have to know
    that conversation memory exists.
    """
    from tests.auth_support import make_organization
    from tests.catalog_support import make_model

    organization = organization or make_organization()
    database = database or MemoryDatabase()
    database.add_organization(organization)

    secret_box = SecretBox(b"0" * 32)
    upstream = (
        make_model(
            organization=None if platform_default else organization,
            name="cheap-distiller",
            secret_box=secret_box,
            credential="sk-distil",
        )
        if with_model
        else None
    )
    if upstream is not None:
        database.add_model(upstream)

    stored = dict(organization.settings or {})
    stored[ORG_DISTILLATION] = {
        **stored.get(ORG_DISTILLATION, {}),
        **({"model_id": str(upstream.id)} if upstream is not None and not platform_default else {}),
        **settings,
    }
    organization.settings = stored

    end_users = end_users or MemoryEndUserStore(database)
    vectors = vectors or MemoryFactVectorStore()
    embedder = embedder or HashEmbedder(dimension=DIMENSION, model="hash-bow")
    debouncer = MemoryDebouncer()
    lock = MemoryLock()
    model = model or ScriptedModel(facts_json({"text": "Works in Rust."}))
    store = MemoryDistillationStore(database)
    registry = CollectorRegistry()
    metrics = build_distillation_metrics(registry)

    resolver = CatalogModelResolver(
        MemoryCatalogStore(database),
        secret_box=secret_box,
        platform_default_id=upstream.id if platform_default and upstream else None,
    )
    reconciler = Reconciler(end_users, vectors=vectors, embedder=embedder, lock=lock)
    distiller = Distiller(
        store,
        end_users=end_users,
        reconciler=reconciler,
        models=resolver,
        proxy=model,
        debouncer=debouncer,
        metrics=metrics,
    )
    trigger = DistillationTrigger(
        _NullQueue(), store=end_users, debouncer=debouncer, ttl_seconds=0.0
    )
    return DistillationFixture(
        database=database,
        organization=organization,
        store=store,
        end_users=end_users,
        vectors=vectors,
        embedder=embedder,
        debouncer=debouncer,
        lock=lock,
        model=model,
        upstream=upstream,
        reconciler=reconciler,
        resolver=resolver,
        distiller=distiller,
        trigger=trigger,
        service=DistillationService(
            store,
            directory=_DirectoryOver(database),
            end_users=end_users,
            distiller=distiller,
            models=resolver,
            debouncer=debouncer,
            cache=trigger,
        ),
        metrics=metrics,
        registry=registry,
    )


class _NullQueue:
    """A queue that accepts everything and remembers it. The trigger's other half is
    asserted on in ``tests/test_distillation_trigger.py``, which uses a real recorder."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def enqueue(self, request: Any) -> str | None:
        self.requests.append(request)
        return str(uuid7())

    async def depth(self) -> int:
        return len(self.requests)

    async def ping(self) -> None:
        return None


def _DirectoryOver(database: MemoryDatabase) -> Any:  # noqa: N802 - a factory, not a class
    """The directory store, over the same rows.

    Imported lazily so this module does not drag the whole directory package into every
    test that only wanted a distiller.
    """
    from app.services.directory_store import MemoryDirectoryStore

    return MemoryDirectoryStore(database)


def days_ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


__all__ = [
    "DIMENSION",
    "SAMPLE_USER_TURN",
    "DistillationFixture",
    "LaggyFactVectors",
    "ModelUnavailable",
    "ScriptedModel",
    "build_distillation",
    "days_ago",
    "facts_json",
]
