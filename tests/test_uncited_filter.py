"""The "nothing cited" filter and rate (task 100), over the in-memory repository.

The contract suite proves the two repositories agree on the fixture; this file pins the
semantics on rows built for the purpose, because the denominator is the whole point and
the fixture only has one request in it.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

from app.core.tenancy import TenantScope
from app.services.memory_db import MemoryDatabase
from app.services.metrics_store import LogFilters, MemoryMetricsRepository
from tests.auth_support import make_organization
from tests.monitoring_support import NOW, make_log_row


def entry(chunk_id: str, *, injected: bool) -> dict[str, object]:
    return {"id": chunk_id, "score": 0.7, "source_name": "h.md", "injected": injected}


async def test_the_filter_and_the_rate_share_the_injected_denominator() -> None:
    database = MemoryDatabase()
    acme = make_organization(name="Acme", slug="acme")
    database.add_organization(acme)
    gateway = uuid.uuid4()
    rows = {
        "cited": make_log_row(
            acme,
            gateway_id=gateway,
            retrieved_chunk_ids=[entry("a", injected=True), entry("b", injected=True)],
            cited_chunk_ids=["b"],
        ),
        "uncited": make_log_row(
            acme, gateway_id=gateway, retrieved_chunk_ids=[entry("a", injected=True)]
        ),
        # Retrieval found something, the budget dropped it: the model had nothing to
        # cite, so this is not an uncited request.
        "all_dropped": make_log_row(
            acme, gateway_id=gateway, retrieved_chunk_ids=[entry("a", injected=False)]
        ),
        "no_memory": make_log_row(acme, gateway_id=gateway),
        # A failure cited nothing because there was no answer.
        "failed": make_log_row(
            acme,
            gateway_id=gateway,
            status_code=502,
            retrieved_chunk_ids=[entry("a", injected=True)],
        ),
    }
    for row in rows.values():
        database.request_logs[row.id] = row
    repository = MemoryMetricsRepository(database)
    window = LogFilters(start=NOW.replace(hour=0), end=NOW.replace(hour=23))

    async with repository.begin(TenantScope.of_organization(acme.id)) as transaction:
        summary = await transaction.summary(window)
        uncited = await transaction.logs(replace(window, uncited=True), after=None, limit=10)
        cited = await transaction.logs(replace(window, uncited=False), after=None, limit=10)

    assert summary.injected_requests == 2
    assert summary.uncited_requests == 1
    assert summary.uncited_rate == 0.5
    assert [row.id for row in uncited] == [rows["uncited"].id]
    assert [row.id for row in cited] == [rows["cited"].id]
