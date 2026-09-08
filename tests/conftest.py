"""Shared fixtures.

Database-backed tests are marked ``db``. They build their schema by running the
**migrations** rather than ``metadata.create_all``, so a broken or missing migration
fails the suite instead of passing against a schema no deployment will ever have.

When no PostgreSQL is reachable the ``db`` fixtures skip rather than fail: the rest of
the suite still runs on a laptop without the stack up, and CI always has the service
container.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.adapters.base import UpstreamTarget
from app.api.control.deps import (
    get_audit_service,
    get_auth_service,
    get_catalog_service,
    get_connector_service,
    get_directory_service,
    get_distillation_service,
    get_end_user_service,
    get_gateway_service,
    get_limits_service,
    get_memory_preview,
    get_monitoring_service,
    get_settings_from_app,
)
from app.api.proxy.deps import (
    get_authenticator,
    get_end_users,
    get_limiter,
    get_memory,
    get_request_logs,
    get_resolver,
)
from app.core.clients import Clients
from app.core.config import Settings, get_settings
from app.core.tenancy import TenantScope
from app.main import create_app
from app.services.embeddings import HashEmbedder
from app.services.end_user_resolver import EndUserResolver, RequestCounters
from app.services.end_user_store import FactDraft, MemoryEndUserStore
from app.services.fact_vectors import FactPoint, MemoryFactVectorStore, fact_payload
from app.services.facts import FactRecaller
from app.services.gateway_resolver import ResolvedGateway
from app.services.memory_db import MemoryDatabase
from app.services.retrieval import MemoryService, Retriever
from app.services.vector_store import ChunkPoint, MemoryVectorStore
from tests.auth_support import PASSWORD, AuthFixture, build_auth
from tests.directory_support import World, build_world
from tests.limits_support import LimitFixture, build_limits, limits_config
from tests.monitoring_support import LogFixture, build_logs
from tests.support import (
    FakeAuthenticator,
    FakeResolver,
    MockUpstream,
    make_gateway,
    make_target,
    serve,
)

TEST_DB_SUFFIX = "_pytest"


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    """A fully wired app instance with its lifespan run.

    Creating the clients opens no sockets, so this works with the stack down; only the
    probes themselves need live services.
    """
    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture
def clients(app: FastAPI) -> Clients:
    result: Clients = app.state.clients
    return result


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------


def _with_database(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


async def _recreate_database(admin_url: str, database: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
            await connection.execute(text(f'CREATE DATABASE "{database}"'))
    finally:
        await engine.dispose()


async def _drop_database(admin_url: str, database: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def database_url(settings_for_session: Settings) -> Iterator[str]:
    """A throwaway database for the whole test session, built from the migrations."""
    base_url = settings_for_session.database_url
    source_name = urlsplit(base_url).path.lstrip("/")
    test_name = f"{source_name}{TEST_DB_SUFFIX}"

    admin_url = _with_database(base_url, "postgres")
    test_url = _with_database(base_url, test_name)

    try:
        asyncio.run(_recreate_database(admin_url, test_name))
    except Exception as exc:  # no server, wrong credentials, no CREATE DATABASE right
        if os.getenv("REQUIRE_DB_TESTS") == "1":
            raise
        pytest.skip(f"PostgreSQL not available for db tests: {exc}")

    alembic_config = AlembicConfig("alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", test_url)
    command.upgrade(alembic_config, "head")

    yield test_url

    asyncio.run(_drop_database(admin_url, test_name))


@pytest.fixture(scope="session")
def settings_for_session() -> Settings:
    return get_settings()


@pytest.fixture
async def db_connection(database_url: str) -> AsyncIterator[AsyncConnection]:
    """An open transaction rolled back after the test, so tests never see each other's
    rows and no cleanup code is needed."""
    engine = create_async_engine(database_url, poolclass=None)
    connection = await engine.connect()
    transaction = await connection.begin()
    try:
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


@pytest.fixture
async def db_session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(bind=db_connection, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def db_session_factory(db_connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """A factory whose sessions join the test's transaction.

    Services take a factory and open their own sessions; without this they would open a
    second connection, outside the test's transaction, and see none of its rows.
    ``create_savepoint`` keeps their ``commit()`` calls from ending the outer transaction
    that the fixture rolls back.
    """
    return async_sessionmaker(
        bind=db_connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class FakeAsync:
    """Callable that records its calls and either returns or raises."""

    def __init__(self, *, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


# ---------------------------------------------------------------------------
# data plane
# ---------------------------------------------------------------------------


@pytest.fixture
async def upstream() -> AsyncIterator[MockUpstream]:
    """A scriptable provider on a real port."""
    mock = MockUpstream()
    async with serve(mock) as base_url:
        mock.base_url = base_url
        yield mock


#: The width of the test embedder. Small, because nothing here is measuring embedding
#: quality — only that the same vectors go in and come out of the same index.
MEMORY_DIMENSION = 64


@dataclass
class MemoryFixture:
    """The memory subsystem over the memory vector store, plus a way to fill it.

    Wired into every proxy harness rather than only the memory tests, so that the data
    plane never reaches for Qdrant even by accident — a gateway with connectors attached
    and no override would otherwise open a socket in the middle of a routing test.

    Both halves, since task 12: the document index and the conversation-memory one, over
    the same embedder. A proxy test that seeds a fact and a chunk is therefore exercising
    the same ``asyncio.gather`` a real request runs, not one branch of it.
    """

    service: MemoryService
    vectors: MemoryVectorStore
    embedder: HashEmbedder
    facts: MemoryFactVectorStore
    end_users: MemoryEndUserStore
    resolver: EndUserResolver
    database: MemoryDatabase

    async def learn(
        self,
        organization_id: uuid.UUID,
        external_id: str,
        *texts: str,
        kind: str = "fact",
        confidence: float = 1.0,
    ) -> uuid.UUID:
        """Remember some facts about somebody, the way the control plane would, and hand
        back the end user's id."""
        scope = TenantScope.of_organization(organization_id)
        async with self.end_users.begin(scope) as transaction:
            end_user = await transaction.touch(external_id)
            rows = [
                await transaction.add_fact(
                    end_user, FactDraft(text=text, kind=kind, confidence=confidence)
                )
                for text in texts
            ]
            await transaction.commit()

        await self.facts.ensure_collection(organization_id, dimension=MEMORY_DIMENSION)
        vectors = await self.embedder.embed([row.text for row in rows])
        await self.facts.upsert(
            organization_id,
            [
                FactPoint(
                    id=str(row.id),
                    vector=list(vector),
                    payload=fact_payload(
                        organization_id=organization_id,
                        end_user_id=end_user.id,
                        kind=row.kind,
                        confidence=float(row.confidence),
                        created_at=row.created_at.timestamp(),
                    ),
                )
                for row, vector in zip(rows, vectors, strict=True)
            ],
        )
        return end_user.id

    async def index(
        self,
        organization_id: uuid.UUID,
        connector_id: uuid.UUID,
        *texts: str,
        source: str = "handbook.md",
        section: str | None = None,
    ) -> uuid.UUID:
        """Put chunks into the index the way ingestion would, and hand back the document
        id so a test can assert on what the drawer would link to."""
        document_id = uuid.uuid4()
        await self.vectors.ensure_collection(organization_id, dimension=MEMORY_DIMENSION)
        vectors = await self.embedder.embed(list(texts))
        await self.vectors.upsert(
            organization_id,
            [
                ChunkPoint(
                    id=f"{document_id}:{index}",
                    vector=vector,
                    payload={
                        "org_id": str(organization_id),
                        "connector_id": str(connector_id),
                        "document_id": str(document_id),
                        "source_name": source,
                        "page_or_section": section,
                        "chunk_index": index,
                        "text": text,
                    },
                )
                for index, (text, vector) in enumerate(zip(texts, vectors, strict=True))
            ],
        )
        return document_id


def build_memory(database: MemoryDatabase | None = None) -> MemoryFixture:
    """The memory subsystem over in-process stores.

    ``database`` is optional so that a caller wiring the data plane and the *control*
    plane together can hand both halves the same rows — which is what makes "the caller
    this request created" and "the caller the monitoring screen names" the same person.
    """
    embedder = HashEmbedder(dimension=MEMORY_DIMENSION, model="hash-bow")
    vectors = MemoryVectorStore()
    facts = MemoryFactVectorStore()
    database = database or MemoryDatabase()
    end_users = MemoryEndUserStore(database)
    retriever = Retriever(embedder, vectors)
    return MemoryFixture(
        service=MemoryService(retriever, recaller=FactRecaller(embedder, facts, end_users)),
        vectors=vectors,
        embedder=embedder,
        facts=facts,
        end_users=end_users,
        # No timer: a proxy test that cares about counters flushes by hand, and a loop
        # running under the others is a source of ordering flakes.
        resolver=EndUserResolver(
            end_users, counters=RequestCounters(end_users, interval_seconds=3600.0)
        ),
        database=database,
    )


@dataclass
class ProxyHarness:
    """Everything a data-plane test needs, wired together."""

    app: FastAPI
    client: AsyncClient
    upstream: MockUpstream
    resolver: FakeResolver
    authenticator: FakeAuthenticator
    token: str
    #: The request log, backed by memory with its timer under the test's control. Every
    #: data-plane test therefore also proves the proxy records what it did — and none of
    #: them opens a database connection to do it.
    logs: LogFixture
    #: Retrieval, over an in-process index. Empty unless a test fills it.
    memory: MemoryFixture
    #: SPEC §11's limiter, over in-process counters. Unlimited unless a test calls
    #: :meth:`limit` — so every data-plane test also proves that an unconfigured gateway
    #: pays nothing for the feature existing.
    limits: LimitFixture

    @property
    def gateway(self) -> ResolvedGateway:
        return self.resolver.gateway

    @property
    def target(self) -> UpstreamTarget:
        return self.gateway.targets[0]

    def url(self, path: str = "/chat/completions", *, slug: str | None = None) -> str:
        return f"/g/{slug or self.gateway.slug}/v1{path}"

    def headers(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token}"}

    def retarget(self, **overrides: Any) -> None:
        """Rebuild the resolved gateway with different upstream settings."""
        target = replace(self.target, **overrides)
        self.resolver.gateway = replace(self.gateway, targets=(target,))

    def limit(self, *, per_end_user: dict[str, int | None] | None = None, **caps: int) -> None:
        """Give the gateway some limits. ``limit(requests_per_minute=2)``."""
        self.resolver.gateway = replace(
            self.gateway, limits=limits_config(per_end_user=per_end_user, **caps)
        )


def build_proxy_app(
    resolver: FakeResolver,
    authenticator: FakeAuthenticator,
    logs: LogFixture,
    memory: MemoryFixture | None = None,
    limits: LimitFixture | None = None,
) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_resolver] = lambda: resolver
    application.dependency_overrides[get_authenticator] = lambda: authenticator
    # Overridden rather than left as the real one: the app's own flusher writes to
    # PostgreSQL, which is not running here, and a background task retrying a connection
    # under every proxy test is noise that hides the failures worth reading.
    application.dependency_overrides[get_request_logs] = lambda: logs.service
    # Same reasoning one service along: the real one talks to Qdrant. Defaulted rather
    # than required, so a test that has no interest in memory does not have to build one
    # — and still cannot reach a socket by forgetting to.
    fixture = memory or build_memory()
    application.dependency_overrides[get_memory] = lambda: fixture.service
    # Same reasoning again: the real resolver writes an end-user row on first sight.
    application.dependency_overrides[get_end_users] = lambda: fixture.resolver
    # And again one service along: the real one is backed by Redis. The limiter itself is
    # the real class — only its counters are in process — so what these tests exercise is
    # the enforcement, not a stand-in for it.
    throttle = limits or build_limits()
    application.dependency_overrides[get_limiter] = lambda: throttle.limiter
    return application


def build_harness_parts(
    upstream: MockUpstream, **target_overrides: Any
) -> tuple[FakeResolver, FakeAuthenticator, str]:
    target = make_target(f"{upstream.base_url}/v1", **target_overrides)
    gateway = make_gateway(target)
    resolver = FakeResolver(gateway=gateway)
    authenticator = FakeAuthenticator()
    return resolver, authenticator, authenticator.issue(gateway.id)


@pytest.fixture
async def proxy(upstream: MockUpstream) -> AsyncIterator[ProxyHarness]:
    resolver, authenticator, token = build_harness_parts(upstream)
    logs = build_logs()
    memory = build_memory()
    limits = build_limits()
    application = build_proxy_app(resolver, authenticator, logs, memory, limits)

    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            yield ProxyHarness(
                app=application,
                client=http_client,
                upstream=upstream,
                resolver=resolver,
                authenticator=authenticator,
                token=token,
                logs=logs,
                memory=memory,
                limits=limits,
            )


@pytest.fixture
async def live_proxy(upstream: MockUpstream) -> AsyncIterator[ProxyHarness]:
    """The same harness, but the gateway is served over a real socket.

    Needed wherever the behaviour under test is about the connection itself — streaming
    timing and client disconnects do not exist in an in-process transport.
    """
    resolver, authenticator, token = build_harness_parts(upstream)
    logs = build_logs()
    memory = build_memory()
    limits = build_limits()
    application = build_proxy_app(resolver, authenticator, logs, memory, limits)

    async with (
        serve(application, lifespan="on") as base_url,
        AsyncClient(base_url=base_url, timeout=30.0) as http_client,
    ):
        yield ProxyHarness(
            app=application,
            client=http_client,
            upstream=upstream,
            resolver=resolver,
            authenticator=authenticator,
            token=token,
            logs=logs,
            memory=memory,
            limits=limits,
        )


async def eventually(condition: Callable[[], bool], *, timeout_seconds: float = 5.0) -> None:
    """Poll until a condition holds. Used where two servers must both notice something."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition did not become true in time")


# ---------------------------------------------------------------------------
# control plane
# ---------------------------------------------------------------------------


@dataclass
class AuthHarness:
    """The real app, with the auth service backed by in-memory storage.

    Only the storage is faked. Routing, cookies, the exception handlers, the
    authenticated-by-default router and the tokens themselves are the production ones.
    """

    app: FastAPI
    client: AsyncClient
    auth: AuthFixture

    async def login(
        self,
        *,
        email: str | None = None,
        password: str | None = None,
        remember: bool = False,
    ) -> Response:
        return await self.client.post(
            "/api/v1/auth/login",
            json={
                "email": email if email is not None else self.auth.user.email,
                "password": password if password is not None else PASSWORD,
                "remember": remember,
            },
        )

    async def sign_in(self) -> str:
        """Log in and return the access token, failing loudly if that did not work."""
        response = await self.login()
        assert response.status_code == 200, response.text
        token: str = response.json()["access_token"]
        return token

    @staticmethod
    def bearer(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}


def build_auth_app(auth: AuthFixture, settings: Settings | None = None) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_auth_service] = lambda: auth.service
    application.dependency_overrides[get_audit_service] = lambda: auth.audit
    application.dependency_overrides[get_directory_service] = lambda: auth.directory
    application.dependency_overrides[get_catalog_service] = lambda: auth.catalog
    connectors = auth.connectors
    if connectors is not None:
        application.dependency_overrides[get_connector_service] = lambda: connectors.service
    end_users = auth.end_users
    if end_users is not None:
        application.dependency_overrides[get_end_user_service] = lambda: end_users.service
    distillation = auth.distillation
    if distillation is not None:
        application.dependency_overrides[get_distillation_service] = lambda: distillation.service
    application.dependency_overrides[get_gateway_service] = lambda: auth.gateways
    application.dependency_overrides[get_limits_service] = lambda: auth.limits
    preview = auth.preview
    if preview is not None:
        application.dependency_overrides[get_memory_preview] = lambda: preview
    application.dependency_overrides[get_monitoring_service] = lambda: auth.monitoring
    if settings is not None:
        application.dependency_overrides[get_settings_from_app] = lambda: settings
    return application


@pytest.fixture
async def auth_harness() -> AsyncIterator[AuthHarness]:
    fixture = build_auth()
    application = build_auth_app(fixture)

    async with application.router.lifespan_context(application):
        # Invitation acceptance opens a session through `app.state`, not through a
        # dependency, because it is not the endpoint's own service. Overriding the
        # dependency alone would leave that call talking to PostgreSQL.
        application.state.auth_service = fixture.service
        application.state.directory_service = fixture.directory
        application.state.catalog_service = fixture.catalog
        if fixture.connectors is not None:
            application.state.connector_service = fixture.connectors.service
        if fixture.end_users is not None:
            application.state.end_user_service = fixture.end_users.service
        if fixture.distillation is not None:
            application.state.distillation_service = fixture.distillation.service
        application.state.gateway_service = fixture.gateways
        application.state.limits_service = fixture.limits
        if fixture.preview is not None:
            application.state.memory_preview = fixture.preview
        application.state.monitoring_service = fixture.monitoring
        # Read off `app.state`, not through a dependency: the support-access recorder is
        # driven by `current_actor`, which has a Request and not a service of its own.
        application.state.support_access = fixture.support_access
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            yield AuthHarness(app=application, client=http_client, auth=fixture)


@dataclass
class DirectoryHarness:
    """The real app over two organizations, signed in as whoever the test asks for.

    Tokens are minted by actually logging in, not forged, so a role check that only holds
    because of how a test built its token cannot pass here.
    """

    app: FastAPI
    client: AsyncClient
    world: World

    async def token_for(self, user: Any) -> str:
        response = await self.client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": PASSWORD, "remember": False},
        )
        assert response.status_code == 200, response.text
        token: str = response.json()["access_token"]
        return token

    async def headers_for(self, user: Any, *, assuming: Any = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {await self.token_for(user)}"}
        if assuming is not None:
            headers["X-Assume-Organization"] = str(assuming)
        return headers

    async def as_user(
        self,
        user: Any,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        assuming: Any = None,
    ) -> Response:
        return await self.client.request(
            method,
            path,
            headers=await self.headers_for(user, assuming=assuming),
            **({} if json_body is None else {"json": json_body}),
        )


@pytest.fixture
async def directory() -> AsyncIterator[DirectoryHarness]:
    world = build_world()
    application = build_auth_app(world.auth)

    async with application.router.lifespan_context(application):
        application.state.auth_service = world.auth.service
        application.state.directory_service = world.directory
        application.state.catalog_service = world.catalog
        if world.auth.connectors is not None:
            application.state.connector_service = world.auth.connectors.service
        if world.auth.end_users is not None:
            application.state.end_user_service = world.auth.end_users.service
        if world.auth.distillation is not None:
            application.state.distillation_service = world.auth.distillation.service
        application.state.gateway_service = world.gateways
        if world.auth.preview is not None:
            application.state.memory_preview = world.auth.preview
        application.state.monitoring_service = world.auth.monitoring
        application.state.support_access = world.auth.support_access
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            yield DirectoryHarness(app=application, client=http_client, world=world)
