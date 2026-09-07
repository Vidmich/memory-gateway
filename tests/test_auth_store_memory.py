"""The store contract, against :class:`MemoryAuthStore`.

The same checks run against PostgreSQL in ``tests/test_auth_db.py``.
"""

from __future__ import annotations

import pytest

from app.services.auth_store import MemoryAuthStore
from tests.auth_store_contract import CHECKS, HASHER, Check, StoreFixture
from tests.auth_support import make_organization, make_user


@pytest.fixture
def store() -> StoreFixture:
    organization = make_organization()
    user = make_user(hasher=HASHER, organization=organization)

    memory = MemoryAuthStore()
    memory.add_organization(organization)
    memory.add_user(user)
    return StoreFixture(store=memory, user=user, organization=organization)


@pytest.mark.parametrize("check", CHECKS, ids=lambda check: check.__name__)
async def test_contract(check: Check, store: StoreFixture) -> None:
    await check(store)
