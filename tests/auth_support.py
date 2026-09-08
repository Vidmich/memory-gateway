"""Builders for control-plane auth tests.

Everything here works against :class:`MemoryAuthStore`, so the login and rotation tests
run on a laptop with nothing installed. ``tests/test_auth_db.py`` runs the same
assertions against PostgreSQL, which is what keeps the in-memory store honest.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import Settings, get_settings
from app.core.crypto import SecretBox
from app.core.ids import uuid7
from app.core.passwords import Hasher, build_hasher
from app.db.models import Organization, User, UserSession
from app.services.auth import AuthService, RequestContext
from app.services.auth_provider import LocalPasswordProvider, PasswordCredentials
from app.services.auth_store import AuthStore, MemoryAuthStore
from app.services.catalog import CatalogService
from app.services.catalog_store import MemoryCatalogStore
from app.services.directory import DirectoryService
from app.services.directory_store import MemoryDirectoryStore
from app.services.gateway_probe import GatewayProbe
from app.services.gateway_store import MemoryGatewayStore
from app.services.gateways import GatewayService
from app.services.login_throttle import LoginThrottle, MemoryThrottleStore
from app.services.memory_db import MemoryDatabase
from app.services.memory_preview import MemoryPreview
from app.services.model_probe import Probe
from app.services.monitoring import MonitoringService
from tests.catalog_support import FakeProbe
from tests.connector_support import TOKENIZER, ConnectorFixture, build_connectors
from tests.distillation_support import DistillationFixture
from tests.distillation_support import build_distillation as build_distillation_fixture
from tests.end_user_support import EndUserFixture, build_end_users
from tests.gateway_support import FakeGatewayProbe, RecordingCache
from tests.monitoring_support import LogFixture, build_logs, build_monitoring

PASSWORD = "correct-horse-battery-staple"
EMAIL = "ada@example.com"
CONTEXT = RequestContext(ip="203.0.113.7", user_agent="pytest")


def make_organization(*, name: str = "Acme", slug: str = "acme") -> Organization:
    return Organization(id=uuid7(), name=name, slug=slug, status="active")


def make_user(
    *,
    hasher: Hasher,
    email: str = EMAIL,
    password: str | None = PASSWORD,
    role: str = "org_admin",
    organization: Organization | None = None,
    status: str = "active",
) -> User:
    return User(
        id=uuid7(),
        organization_id=organization.id if organization else None,
        email=email,
        password_hash=hasher.hash(password) if password is not None else None,
        role=role,
        name="Ada Lovelace",
        status=status,
        last_login_at=None,
    )


@dataclass
class AuthFixture:
    """A service wired to in-memory everything, plus the user it was built around.

    ``directory`` and ``catalog`` share the same :class:`MemoryDatabase`, because the
    flows that matter cross them — accepting an invitation creates a member through the
    directory and then opens a session through auth, and a model is only isolated
    relative to the organizations the directory created.
    """

    service: AuthService
    directory: DirectoryService
    catalog: CatalogService
    gateways: GatewayService
    #: Task 09's whole ingestion stack over the same rows, so a cross-tenant test can aim
    #: at a connector and a document that genuinely exist.
    connectors: ConnectorFixture | None
    #: Task 10's editor previews, over the same gateway rows and the same vector index
    #: the connector fixture writes to. ``None`` when there is no organization, for the
    #: same reason ``connectors`` is: neither has anything to be scoped to.
    preview: MemoryPreview | None
    #: Task 12's conversation memory over the same rows: end users, their facts, and the
    #: recaller a request would run. ``None`` without an organization, as above.
    end_users: EndUserFixture | None
    #: Task 13's write half, over the same rows. ``None`` for a platform-only fixture,
    #: which has no organization for a conversation to belong to.
    distillation: DistillationFixture | None
    #: The read half of task 07, over the same rows the write half fills in.
    monitoring: MonitoringService
    logs: LogFixture
    secret_box: SecretBox
    probe: FakeProbe
    gateway_probe: FakeGatewayProbe
    #: Shared by the catalog and the gateway service, as the real ``GatewayCache`` is —
    #: so a test can assert that editing a *model* invalidated a gateway's config.
    cache: RecordingCache
    store: MemoryAuthStore
    database: MemoryDatabase
    throttle_store: MemoryThrottleStore
    hasher: Hasher
    settings: Settings
    user: User
    organization: Organization | None

    def credentials(
        self, *, email: str | None = None, password: str | None = None
    ) -> PasswordCredentials:
        return PasswordCredentials(
            email=email if email is not None else self.user.email,
            password=password if password is not None else PASSWORD,
        )

    def sessions_of(self, family_id: uuid.UUID) -> list[UserSession]:
        return [record for record in self.store.sessions.values() if record.family_id == family_id]

    def expire(self, token_hash: str) -> None:
        """Push a stored refresh token's expiry into the past."""
        for record in self.store.sessions.values():
            if record.refresh_token_hash == token_hash:
                record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                return
        raise AssertionError("no session with that token hash")


def build_auth(
    *,
    settings: Settings | None = None,
    hasher: Hasher | None = None,
    store: AuthStore | None = None,
    user: User | None = None,
    organization: Organization | None = None,
    with_organization: bool = True,
    database: MemoryDatabase | None = None,
    probe: Probe | None = None,
    gateway_probe: GatewayProbe | None = None,
) -> AuthFixture:
    settings = settings or get_settings()
    hasher = hasher or build_hasher(settings)
    database = database or MemoryDatabase()
    memory_store = MemoryAuthStore(database)

    if organization is None and with_organization:
        organization = make_organization()
    if organization is not None:
        memory_store.add_organization(organization)

    user = user or make_user(hasher=hasher, organization=organization)
    memory_store.add_user(user)

    throttle_store = MemoryThrottleStore()
    service = AuthService(
        store or memory_store,
        provider=LocalPasswordProvider(hasher),
        hasher=hasher,
        throttle=LoginThrottle(throttle_store, settings),
        settings=settings,
    )
    directory = DirectoryService(
        MemoryDirectoryStore(database),
        hasher=hasher,
        settings=settings,
    )
    secret_box = SecretBox.from_settings(settings)
    fake_probe = FakeProbe()
    cache = RecordingCache()
    catalog = CatalogService(
        MemoryCatalogStore(database),
        secret_box=secret_box,
        probe=probe or fake_probe,
        cache=cache,
        # No limiter: rate limiting is asserted directly in tests/test_rate_limit.py, and
        # a counter shared across a test module would make every other test order-dependent.
        settings=settings,
    )
    logs = build_logs(database=database)
    monitoring = build_monitoring(database)
    fake_gateway_probe = FakeGatewayProbe()
    gateway_store = MemoryGatewayStore(database)
    gateways = GatewayService(
        gateway_store,
        probe=gateway_probe or fake_gateway_probe,
        cache=cache,
        settings=settings,
    )
    connectors = (
        build_connectors(organization, database=database, settings=settings)
        if organization is not None
        else None
    )
    preview = (
        MemoryPreview(gateway_store, memory=connectors.memory, tokenizer=TOKENIZER)
        if connectors is not None
        else None
    )
    end_users = (
        build_end_users(organization, database=database) if organization is not None else None
    )
    distillation = (
        build_distillation_fixture(
            organization,
            database=database,
            # The same store, index and embedder the memory browser uses. A distillation
            # that wrote through a second set would deduplicate against an index the
            # screens cannot see.
            end_users=end_users.store,
            vectors=end_users.vectors,
            embedder=end_users.embedder,
            with_model=False,
        )
        if organization is not None and end_users is not None
        else None
    )
    return AuthFixture(
        service=service,
        directory=directory,
        catalog=catalog,
        gateways=gateways,
        connectors=connectors,
        preview=preview,
        end_users=end_users,
        distillation=distillation,
        monitoring=monitoring,
        logs=logs,
        secret_box=secret_box,
        probe=fake_probe,
        gateway_probe=fake_gateway_probe,
        cache=cache,
        store=memory_store,
        database=database,
        throttle_store=throttle_store,
        hasher=hasher,
        settings=settings,
        user=user,
        organization=organization,
    )
