"""The audit-store contract, against :class:`MemoryAuditStore`.

The same checks run against PostgreSQL in ``tests/test_audit_db.py``, which also proves
the half no in-memory store can: that the database itself refuses an ``UPDATE``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.ids import uuid7
from app.services.audit_store import MemoryAuditStore
from app.services.memory_db import MemoryDatabase
from tests.audit_store_contract import CHECKS, NOW, Check, Fixture, make_event


def build_fixture() -> tuple[MemoryDatabase, Fixture]:
    """Shared with the PostgreSQL half, which inserts the same rows.

    The two Acme events are minted in age order so their UUIDv7 ids sort the way their
    timestamps do — which is the assumption cursor pagination is built on, and one a
    fixture that shuffled them would quietly break.
    """
    acme, globex = uuid7(), uuid7()
    ada = uuid7()

    acme_older = make_event(
        organization_id=acme,
        action="key.revoke",
        target_type="api_key",
        target_label="production",
        created_at=NOW - timedelta(hours=2),
    )
    acme_event = make_event(organization_id=acme, actor_user_id=ada, created_at=NOW)
    globex_event = make_event(organization_id=globex, created_at=NOW)
    platform_event = make_event(
        organization_id=None,
        action="model.create",
        target_type="upstream_model",
        target_label="shared-gpt-4o",
        created_at=NOW,
    )

    database = MemoryDatabase()
    for event in (acme_older, acme_event, globex_event, platform_event):
        database.add_audit_event(event)

    return database, Fixture(
        store=MemoryAuditStore(database),
        acme=acme,
        globex=globex,
        acme_event=acme_event,
        acme_older=acme_older,
        globex_event=globex_event,
        platform_event=platform_event,
    )


@pytest.fixture
def fixture() -> Fixture:
    return build_fixture()[1]


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, fixture: Fixture) -> None:
    await check(fixture)
