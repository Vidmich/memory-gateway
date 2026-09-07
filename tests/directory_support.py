"""A two-tenant world, built once and used by every tenancy test.

Two organizations with overlapping shapes is the minimum that can catch an isolation bug:
one organization alone makes every query look correctly scoped, because there is nothing
else it could have returned.

Everything runs against :class:`MemoryDatabase`, shared by the auth and directory stores,
so these tests need no PostgreSQL. ``tests/test_directory_db.py`` runs the contract
against the real one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.core.config import Settings, get_settings
from app.core.passwords import Hasher, build_hasher
from app.core.tenancy import TenantScope
from app.db.models import Organization, User
from app.services.directory import Actor, DirectoryService
from app.services.memory_db import MemoryDatabase
from tests.auth_support import PASSWORD, AuthFixture, build_auth, make_organization, make_user


@dataclass
class World:
    """Two organizations, one of every role, and a platform account."""

    settings: Settings
    hasher: Hasher
    database: MemoryDatabase
    directory: DirectoryService
    auth: AuthFixture

    acme: Organization
    globex: Organization

    superadmin: User
    acme_admin: User
    acme_member: User
    acme_viewer: User
    globex_admin: User

    #: Every user, keyed by the short name the tests use.
    people: dict[str, User] = field(default_factory=dict)

    def actor(self, user: User) -> Actor:
        return Actor(user_id=user.id, scope=TenantScope.of_user(user))

    def platform_actor_assuming(self, organization_id: uuid.UUID) -> Actor:
        scope = TenantScope.of_user(self.superadmin)
        return Actor(
            user_id=self.superadmin.id,
            scope=scope.assume(organization_id, actor_user_id=self.superadmin.id),
        )


def build_world(*, settings: Settings | None = None) -> World:
    settings = settings or get_settings()
    hasher = build_hasher(settings)
    database = MemoryDatabase()

    acme = make_organization(name="Acme", slug="acme")
    globex = make_organization(name="Globex", slug="globex")
    database.add_organization(acme)
    database.add_organization(globex)

    people = {
        "superadmin": make_user(
            hasher=hasher, email="root@example.com", role="superadmin", organization=None
        ),
        "acme_admin": make_user(
            hasher=hasher, email="admin@acme.example.com", role="org_admin", organization=acme
        ),
        "acme_member": make_user(
            hasher=hasher, email="member@acme.example.com", role="org_member", organization=acme
        ),
        "acme_viewer": make_user(
            hasher=hasher, email="viewer@acme.example.com", role="org_viewer", organization=acme
        ),
        "globex_admin": make_user(
            hasher=hasher, email="admin@globex.example.com", role="org_admin", organization=globex
        ),
    }
    for person in people.values():
        database.add_user(person)

    # `build_auth` wires the login service to the same rows; the user it is "built
    # around" only matters for the convenience helpers on the fixture.
    auth = build_auth(
        settings=settings,
        hasher=hasher,
        database=database,
        user=people["acme_admin"],
        organization=acme,
    )

    return World(
        settings=settings,
        hasher=hasher,
        database=database,
        # The same instance the app's dependency override hands out, so a test cannot
        # accidentally exercise two services over one database.
        directory=auth.directory,
        auth=auth,
        acme=acme,
        globex=globex,
        superadmin=people["superadmin"],
        acme_admin=people["acme_admin"],
        acme_member=people["acme_member"],
        acme_viewer=people["acme_viewer"],
        globex_admin=people["globex_admin"],
        people=people,
    )


__all__ = ["PASSWORD", "World", "build_world"]
