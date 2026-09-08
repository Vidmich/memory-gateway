"""Builders for end-user and conversation-memory tests.

The same principle as ``tests/connector_support.py``: every collaborator is the *second
implementation of a port* rather than a mock, so a test here drives the real
:class:`~app.services.facts.FactRecaller` against the real
:class:`~app.services.end_users.EndUserService` and the only thing swapped out is the two
sockets — PostgreSQL and Qdrant.

That matters most for one assertion in this task. "Facts recalled for alice are never
recalled for bob" has to be true of the code that runs in production, and it is only
meaningfully tested if the thing under test is the actual filter chain: a vector search
scoped to a collection *and* an ``end_user_id``, followed by a row read scoped to an
organization *and* the same ``end_user_id``. A mock that returns a canned list proves
nothing about any of that.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.core.ids import uuid7
from app.core.tenancy import Actor, TenantScope
from app.db.models import EndUser, MemoryFact, Organization
from app.schemas.distillation import ORG_DISTILLATION
from app.services.embeddings import HashEmbedder
from app.services.end_user_resolver import EndUserResolver, RequestCounters
from app.services.end_user_store import MemoryEndUserStore
from app.services.end_users import EndUserService
from app.services.fact_vectors import MemoryFactVectorStore
from app.services.facts import FactRecaller
from app.services.memory_db import MemoryDatabase
from app.services.metrics_store import MemoryMetricsRepository

#: Same width as the connector fixture's embedder, so a test that wires both halves of
#: memory into one gateway does not have to think about two dimensions.
DIMENSION = 64


def make_end_user(
    organization: Organization,
    *,
    external_id: str = "alice",
    label: str | None = None,
    request_count: int = 0,
) -> EndUser:
    now = datetime.now(UTC)
    return EndUser(
        id=uuid7(),
        organization_id=organization.id,
        external_id=external_id,
        label=label,
        first_seen_at=now,
        last_seen_at=now,
        request_count=request_count,
    )


def make_fact(
    end_user: EndUser,
    *,
    text: str = "Prefers Python.",
    kind: str = "preference",
    confidence: float = 1.0,
    superseded: bool = False,
    expires_in_days: float | None = None,
    age_days: float = 0.0,
) -> MemoryFact:
    """A fact row without going through the service, for tests that only need one to
    exist — the cross-tenant net above all, which needs a *foreign* id that is real."""
    now = datetime.now(UTC)
    seen = now - timedelta(days=age_days)
    return MemoryFact(
        id=uuid7(),
        organization_id=end_user.organization_id,
        end_user_id=end_user.id,
        text=text,
        kind=kind,
        confidence=confidence,
        superseded_at=now if superseded else None,
        expires_at=None if expires_in_days is None else now + timedelta(days=expires_in_days),
        created_at=seen,
        last_seen_at=seen,
    )


@dataclass
class EndUserFixture:
    """The whole conversation-memory stack over memory, plus the rows it was built around."""

    database: MemoryDatabase
    store: MemoryEndUserStore
    vectors: MemoryFactVectorStore
    embedder: HashEmbedder
    counters: RequestCounters
    resolver: EndUserResolver
    recaller: FactRecaller
    service: EndUserService
    organization: Organization
    user_id: uuid.UUID = field(default_factory=uuid7)

    @property
    def actor(self) -> Actor:
        return Actor(
            user_id=self.user_id,
            scope=TenantScope(role="org_admin", organization_id=self.organization.id),
        )

    @property
    def organization_id(self) -> uuid.UUID:
        return self.organization.id

    async def end_user(self, external_id: str = "alice") -> EndUser:
        """Create or fetch, the way a request would."""
        async with self.store.begin(self.actor.scope) as transaction:
            return await transaction.touch(external_id)

    async def remember(self, end_user: EndUser, *texts: str, **kwargs: object) -> list[MemoryFact]:
        """Write facts through the *service*, so each one is indexed the way a real one is."""
        created = []
        for text in texts:
            created.append(
                await self.service.create_fact(
                    self.actor,
                    end_user.id,
                    text=text,
                    kind=str(kwargs.get("kind", "fact")),
                    confidence=float(kwargs.get("confidence", 1.0)),  # type: ignore[arg-type]
                )
            )
        return created

    async def facts_of(self, end_user: EndUser) -> list[MemoryFact]:
        page = await self.service.list_facts(self.actor, end_user.id, limit=200)
        return list(page.items)

    async def vector_count(self, end_user: EndUser | None = None) -> int:
        return await self.vectors.count(
            self.organization_id, end_user_id=end_user.id if end_user else None
        )


def build_end_users(
    organization: Organization,
    *,
    database: MemoryDatabase | None = None,
    vectors: MemoryFactVectorStore | None = None,
    embedder: HashEmbedder | None = None,
    logs: MemoryMetricsRepository | None = None,
    dimension: int = DIMENSION,
    max_facts_per_user: int = 500,
) -> EndUserFixture:
    database = database or MemoryDatabase()
    # The organization row has to be *in* the database, not merely passed alongside it:
    # the per-person fact bound lives in `organizations.settings`, and a fixture that kept
    # the row outside would silently test the default whatever it was asked for.
    organization.settings = {
        **(organization.settings or {}),
        ORG_DISTILLATION: {
            **(organization.settings or {}).get(ORG_DISTILLATION, {}),
            "max_facts_per_user": max_facts_per_user,
        },
    }
    database.add_organization(organization)
    store = MemoryEndUserStore(database)
    vectors = vectors or MemoryFactVectorStore()
    embedder = embedder or HashEmbedder(dimension=dimension, model="hash-bow")
    # A long interval and no `start()`: every test that cares about counters flushes by
    # hand, and a timer running under the others is a source of ordering flakes.
    counters = RequestCounters(store, interval_seconds=3600.0)
    return EndUserFixture(
        database=database,
        store=store,
        vectors=vectors,
        embedder=embedder,
        counters=counters,
        resolver=EndUserResolver(store, counters=counters),
        recaller=FactRecaller(embedder, vectors, store),
        service=EndUserService(
            store,
            vectors=vectors,
            embedder=embedder,
            logs=logs or MemoryMetricsRepository(database),
        ),
        organization=organization,
    )


__all__ = [
    "DIMENSION",
    "EndUserFixture",
    "build_end_users",
    "make_end_user",
    "make_fact",
]
