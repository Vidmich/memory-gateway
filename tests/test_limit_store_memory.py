"""The in-process limit store against the shared contract.

Fast, and the one every other rate-limiting test runs against. Its twin —
``tests/test_limit_store_redis.py`` — runs the identical checks against the Lua script, so
a behaviour that only holds here is a failure rather than a discovery.
"""

from __future__ import annotations

from app.services.limit_store import MemoryLimitStore
from tests.limit_store_contract import check_store


async def test_the_memory_store_satisfies_the_contract() -> None:
    await check_store(MemoryLimitStore())
