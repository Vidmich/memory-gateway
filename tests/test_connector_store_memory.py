"""The connector-store contract, against :class:`MemoryConnectorStore`.

The same checks run against PostgreSQL in ``tests/test_connector_db.py``.
"""

from __future__ import annotations

import pytest

from app.services.connector_store import MemoryConnectorStore
from app.services.memory_db import MemoryDatabase
from tests.auth_support import make_organization
from tests.connector_store_contract import CHECKS, Check, Fixture
from tests.connector_support import make_connector


def build_fixture() -> tuple[MemoryDatabase, Fixture]:
    """Shared with the PostgreSQL half, which inserts the same rows."""
    acme = make_organization(name="Acme", slug="acme")
    globex = make_organization(name="Globex", slug="globex")

    acme_connector = make_connector(acme, name="Product docs")
    globex_connector = make_connector(globex, name="Their docs")

    database = MemoryDatabase()
    for organization in (acme, globex):
        database.add_organization(organization)
    for connector in (acme_connector, globex_connector):
        database.add_connector(connector)

    return database, Fixture(
        store=MemoryConnectorStore(database),
        acme=acme,
        globex=globex,
        acme_connector=acme_connector,
        globex_connector=globex_connector,
    )


@pytest.fixture
def fixture() -> Fixture:
    return build_fixture()[1]


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    await check(fixture)
