"""The gateway-store contract, against :class:`MemoryGatewayStore`.

The same checks run against PostgreSQL in ``tests/test_gateway_db.py``.
"""

from __future__ import annotations

import pytest

from app.services.gateway_store import MemoryGatewayStore
from app.services.memory_db import MemoryDatabase
from tests.auth_support import make_organization
from tests.catalog_support import make_model, make_target_row
from tests.gateway_store_contract import CHECKS, Check, Fixture
from tests.gateway_support import make_gateway_row, make_key_row


def build_fixture() -> tuple[MemoryDatabase, Fixture]:
    """Shared with the PostgreSQL half, which inserts the same rows."""
    acme = make_organization(name="Acme", slug="acme")
    globex = make_organization(name="Globex", slug="globex")

    acme_model = make_model(organization=acme, name="acme-gpt")
    globex_model = make_model(organization=globex, name="globex-gpt")
    global_model = make_model(organization=None, name="shared-gpt-4o")

    acme_gateway = make_gateway_row(acme, slug="acme-chat")
    globex_gateway = make_gateway_row(globex, slug="globex-chat")
    acme_key, _ = make_key_row(acme_gateway.id, name="acme production")
    globex_key, _ = make_key_row(globex_gateway.id, name="globex production")

    database = MemoryDatabase()
    for organization in (acme, globex):
        database.add_organization(organization)
    for model in (acme_model, globex_model, global_model):
        database.add_model(model)
    for gateway in (acme_gateway, globex_gateway):
        database.add_gateway(gateway)
    database.add_target(make_target_row(acme_gateway.id, acme_model.id))
    database.add_target(make_target_row(globex_gateway.id, globex_model.id))
    for key in (acme_key, globex_key):
        database.add_key(key)

    return database, Fixture(
        store=MemoryGatewayStore(database),
        acme=acme,
        globex=globex,
        acme_model=acme_model,
        globex_model=globex_model,
        global_model=global_model,
        acme_gateway=acme_gateway,
        globex_gateway=globex_gateway,
        acme_key=acme_key,
        globex_key=globex_key,
    )


@pytest.fixture
def fixture() -> Fixture:
    return build_fixture()[1]


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    await check(fixture)
