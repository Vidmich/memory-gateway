"""The end-user-store contract, against :class:`MemoryEndUserStore`.

The same checks run against PostgreSQL in ``tests/test_end_user_db.py``.
"""

from __future__ import annotations

import pytest

from app.services.end_user_store import MemoryEndUserStore
from app.services.memory_db import MemoryDatabase
from tests.auth_support import make_organization
from tests.end_user_store_contract import CHECKS, Check, Fixture
from tests.end_user_support import make_end_user


def build_fixture() -> tuple[MemoryDatabase, Fixture]:
    """Shared with the PostgreSQL half, which inserts the same rows."""
    acme = make_organization(name="Acme", slug="acme")
    globex = make_organization(name="Globex", slug="globex")

    # Both called "alice": the same external id in two organizations is the ordinary
    # case, and a lookup keyed on it alone would find the wrong person.
    acme_end_user = make_end_user(acme, external_id="alice")
    globex_end_user = make_end_user(globex, external_id="alice")

    database = MemoryDatabase()
    for organization in (acme, globex):
        database.add_organization(organization)
    for end_user in (acme_end_user, globex_end_user):
        database.add_end_user(end_user)

    return database, Fixture(
        store=MemoryEndUserStore(database),
        acme=acme,
        globex=globex,
        acme_end_user=acme_end_user,
        globex_end_user=globex_end_user,
    )


@pytest.fixture
def fixture() -> Fixture:
    return build_fixture()[1]


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    await check(fixture)
