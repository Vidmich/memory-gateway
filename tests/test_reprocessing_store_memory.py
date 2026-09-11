"""The reprocessing store contract, against the in-memory twins (task 104)."""

from __future__ import annotations

import pytest

from app.core.ids import uuid7
from app.db.models import Organization
from app.services.connector_store import MemoryConnectorStore
from app.services.memory_db import MemoryDatabase
from app.services.reprocessing_store import MemoryReprocessingStore
from tests.connector_support import make_connector
from tests.reprocessing_store_contract import CHECKS, Check, Fixture

pytestmark = pytest.mark.anyio


def _organization(name: str, slug: str) -> Organization:
    return Organization(id=uuid7(), name=name, slug=slug, status="active", settings={})


def build_fixture() -> tuple[MemoryDatabase, Fixture]:
    """Two organizations with a connector each. The checks add the documents and runs."""
    database = MemoryDatabase()
    acme = _organization("Acme", "acme")
    globex = _organization("Globex", "globex")
    database.add_organization(acme)
    database.add_organization(globex)
    acme_connector = make_connector(acme, name="Policies")
    globex_connector = make_connector(globex, name="Globex docs")
    for connector in (acme_connector, globex_connector):
        database.add_connector(connector)
    return database, Fixture(
        connectors=MemoryConnectorStore(database),
        runs=MemoryReprocessingStore(database),
        acme=acme,
        globex=globex,
        acme_connector=acme_connector,
        globex_connector=globex_connector,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check) -> None:
    _, fixture = build_fixture()
    await check(fixture)
