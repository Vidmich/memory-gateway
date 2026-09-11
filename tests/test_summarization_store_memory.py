"""The summarization ledger's contract, against the in-memory twin (task 102)."""

from __future__ import annotations

import pytest

from app.core.ids import uuid7
from app.db.models import Organization
from app.services.memory_db import MemoryDatabase
from app.services.summarization_store import MemorySummarizationStore
from tests.connector_support import make_connector, make_document
from tests.summarization_store_contract import CHECKS, WAITING_ON_CAP, Check, Fixture

pytestmark = pytest.mark.anyio


def _organization(name: str, slug: str) -> Organization:
    return Organization(id=uuid7(), name=name, slug=slug, status="active", settings={})


def build_fixture() -> tuple[MemoryDatabase, Fixture]:
    """The rows both halves of the contract are run against: two organizations, three
    connectors, and the documents whose ``reason`` says they are waiting on a cap."""
    database = MemoryDatabase()
    acme = _organization("Acme", "acme")
    globex = _organization("Globex", "globex")
    database.add_organization(acme)
    database.add_organization(globex)

    acme_connector = make_connector(acme, name="Policies")
    acme_other = make_connector(acme, name="Repository")
    globex_connector = make_connector(globex, name="Globex docs")
    for connector in (acme_connector, acme_other, globex_connector):
        database.add_connector(connector)

    parked_one = make_document(acme_connector, name="one.md", status="pending", chunk_count=0)
    parked_two = make_document(acme_connector, name="two.md", status="pending", chunk_count=0)
    for document in (parked_one, parked_two):
        document.reason = WAITING_ON_CAP
    # Pending for another reason, indexed, and parked under another tenant: none count.
    merely_pending = make_document(acme_other, name="three.md", status="pending", chunk_count=0)
    indexed = make_document(acme_connector, name="four.md")
    foreign = make_document(globex_connector, name="five.md", status="pending", chunk_count=0)
    foreign.reason = WAITING_ON_CAP
    for document in (parked_one, parked_two, merely_pending, indexed, foreign):
        database.documents[document.id] = document

    return database, Fixture(
        store=MemorySummarizationStore(database),
        acme=acme,
        globex=globex,
        acme_connector=acme_connector,
        acme_other=acme_other,
        globex_connector=globex_connector,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check) -> None:
    _, fixture = build_fixture()
    await check(fixture)
