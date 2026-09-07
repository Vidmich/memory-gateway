"""The directory-store contract, against :class:`MemoryDirectoryStore`.

The same checks run against PostgreSQL in ``tests/test_directory_db.py``.
"""

from __future__ import annotations

import pytest

from app.core.passwords import Hasher
from app.services.directory_store import MemoryDirectoryStore
from app.services.memory_db import MemoryDatabase
from tests.auth_support import make_organization, make_user
from tests.directory_store_contract import CHECKS, Check, Fixture, make_invitation

#: Weak on purpose: these checks are about scoping, not about Argon2.
HASHER = Hasher(time_cost=1, memory_cost_kib=8, parallelism=1)


def build_fixture() -> tuple[MemoryDatabase, Fixture]:
    """Shared with the PostgreSQL half, which inserts the same rows."""
    acme = make_organization(name="Acme", slug="acme")
    globex = make_organization(name="Globex", slug="globex")

    acme_admin = make_user(
        hasher=HASHER, email="admin@acme.example.com", role="org_admin", organization=acme
    )
    acme_viewer = make_user(
        hasher=HASHER, email="viewer@acme.example.com", role="org_viewer", organization=acme
    )
    globex_admin = make_user(
        hasher=HASHER, email="admin@globex.example.com", role="org_admin", organization=globex
    )
    superadmin = make_user(
        hasher=HASHER, email="root@example.com", role="superadmin", organization=None
    )
    acme_invitation = make_invitation(acme, email="invitee@example.com")
    globex_invitation = make_invitation(globex, email="elsewhere@example.com")

    database = MemoryDatabase()
    for organization in (acme, globex):
        database.add_organization(organization)
    for user in (acme_admin, acme_viewer, globex_admin, superadmin):
        database.add_user(user)
    for invitation in (acme_invitation, globex_invitation):
        database.add_invitation(invitation)

    return database, Fixture(
        store=MemoryDirectoryStore(database),
        acme=acme,
        globex=globex,
        acme_admin=acme_admin,
        acme_viewer=acme_viewer,
        globex_admin=globex_admin,
        superadmin=superadmin,
        acme_invitation=acme_invitation,
        globex_invitation=globex_invitation,
    )


@pytest.fixture
def fixture() -> Fixture:
    return build_fixture()[1]


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    await check(fixture)
