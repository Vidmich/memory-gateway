"""The catalog-store contract, against :class:`MemoryCatalogStore`.

The same checks run against PostgreSQL in ``tests/test_catalog_db.py``.
"""

from __future__ import annotations

import pytest

from app.services.catalog_store import MemoryCatalogStore
from app.services.memory_db import MemoryDatabase
from tests.auth_support import make_organization
from tests.catalog_store_contract import CHECKS, Check, Fixture
from tests.catalog_support import make_gateway_row, make_model, make_target_row


def build_fixture() -> tuple[MemoryDatabase, Fixture]:
    """Shared with the PostgreSQL half, which inserts the same rows."""
    acme = make_organization(name="Acme", slug="acme")
    globex = make_organization(name="Globex", slug="globex")

    acme_model = make_model(organization=acme, name="acme-gpt")
    globex_model = make_model(organization=globex, name="globex-gpt")
    global_model = make_model(organization=None, name="shared-gpt-4o")
    acme_gateway = make_gateway_row(acme, slug="acme-chat")

    database = MemoryDatabase()
    for organization in (acme, globex):
        database.add_organization(organization)
    for model in (acme_model, globex_model, global_model):
        database.add_model(model)
    database.add_gateway(acme_gateway)
    database.add_target(make_target_row(acme_gateway.id, acme_model.id))

    return database, Fixture(
        store=MemoryCatalogStore(database),
        acme=acme,
        globex=globex,
        acme_model=acme_model,
        globex_model=globex_model,
        global_model=global_model,
        acme_gateway=acme_gateway,
    )


@pytest.fixture
def fixture() -> Fixture:
    return build_fixture()[1]


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    await check(fixture)
