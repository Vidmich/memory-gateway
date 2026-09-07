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
from app.core.ids import uuid7
from app.core.passwords import Hasher, build_hasher
from app.db.models import Organization, User, UserSession
from app.services.auth import AuthService, RequestContext
from app.services.auth_provider import LocalPasswordProvider, PasswordCredentials
from app.services.auth_store import AuthStore, MemoryAuthStore
from app.services.directory import DirectoryService
from app.services.directory_store import MemoryDirectoryStore
from app.services.login_throttle import LoginThrottle, MemoryThrottleStore
from app.services.memory_db import MemoryDatabase

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

    ``directory`` shares the same :class:`MemoryDatabase`, because the flows that matter
    cross both — accepting an invitation creates a member through the directory and then
    opens a session through auth.
    """

    service: AuthService
    directory: DirectoryService
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
    return AuthFixture(
        service=service,
        directory=directory,
        store=memory_store,
        database=database,
        throttle_store=throttle_store,
        hasher=hasher,
        settings=settings,
        user=user,
        organization=organization,
    )
