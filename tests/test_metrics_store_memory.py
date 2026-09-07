"""The in-memory metrics repository, against the shared contract.

Runs on a laptop with nothing installed, which is the only reason the aggregation is
covered at all here: ``tests/test_metrics_db.py`` runs the same list against PostgreSQL in
CI, and a divergence between the two shows up as one of these checks failing on one side.
"""

from __future__ import annotations

import pytest

from app.services.memory_db import MemoryDatabase
from app.services.metrics_store import MemoryMetricsRepository
from tests.auth_support import make_organization
from tests.metrics_store_contract import CHECKS, Check, Fixture
from tests.monitoring_support import metrics_seed


@pytest.fixture
def fixture() -> Fixture:
    database = MemoryDatabase()
    acme = make_organization(name="Acme", slug="acme")
    globex = make_organization(name="Globex", slug="globex")
    database.add_organization(acme)
    database.add_organization(globex)

    seed = metrics_seed(acme, globex)
    for row in seed.logs:
        database.request_logs[row.id] = row
    for transcript in seed.transcripts:
        database.transcripts[transcript.request_log_id] = transcript

    return Fixture(
        repository=MemoryMetricsRepository(database),
        acme=acme,
        globex=globex,
        acme_gateway_id=seed.acme_gateway_id,
        other_gateway_id=seed.other_gateway_id,
        globex_gateway_id=seed.globex_gateway_id,
        acme_log_id=seed.acme_log_id,
        bodiless_log_id=seed.bodiless_log_id,
        globex_log_id=seed.globex_log_id,
    )


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    await check(fixture)
