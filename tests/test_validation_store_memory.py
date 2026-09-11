"""The validation stores' contract, against the in-memory twins (task 103)."""

from __future__ import annotations

import pytest

from app.core.ids import uuid7
from app.db.models import Organization
from app.services.evaluation_store import MemoryEvaluationStore
from app.services.index_audit_store import MemoryIndexAuditStore
from app.services.memory_db import MemoryDatabase
from tests.connector_support import make_connector
from tests.gateway_support import make_gateway_row
from tests.validation_store_contract import CHECKS, Check, Fixture

pytestmark = pytest.mark.anyio


def _organization(name: str, slug: str) -> Organization:
    return Organization(id=uuid7(), name=name, slug=slug, status="active", settings={})


def build_fixture() -> tuple[MemoryDatabase, Fixture]:
    """Two organizations, a connector and a gateway each. The checks add the rest."""
    database = MemoryDatabase()
    acme = _organization("Acme", "acme")
    globex = _organization("Globex", "globex")
    database.add_organization(acme)
    database.add_organization(globex)
    acme_connector = make_connector(acme, name="Policies")
    globex_connector = make_connector(globex, name="Globex docs")
    for connector in (acme_connector, globex_connector):
        database.add_connector(connector)
    acme_gateway = make_gateway_row(acme, slug="acme-support")
    globex_gateway = make_gateway_row(globex, slug="globex-support")
    for gateway in (acme_gateway, globex_gateway):
        database.add_gateway(gateway)
    return database, Fixture(
        audits=MemoryIndexAuditStore(database),
        evaluations=MemoryEvaluationStore(database),
        acme=acme,
        globex=globex,
        acme_connector=acme_connector,
        globex_connector=globex_connector,
        acme_gateway=acme_gateway,
        globex_gateway=globex_gateway,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check) -> None:
    _, fixture = build_fixture()
    await check(fixture)
