"""The migration history must run forwards and backwards cleanly.

These are synchronous tests on purpose: Alembic's env.py drives its own event loop, so it
cannot be invoked from inside a running one.
"""

from __future__ import annotations

import asyncio

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.db


def _config(database_url: str) -> AlembicConfig:
    config = AlembicConfig("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


async def _current_version(database_url: str) -> str | None:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            value = result.scalar()
            return str(value) if value is not None else None
    finally:
        await engine.dispose()


def test_upgrade_downgrade_round_trip(database_url: str) -> None:
    config = _config(database_url)

    command.downgrade(config, "base")
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    assert (
        asyncio.run(_current_version(database_url))
        == ScriptDirectory.from_config(config).get_current_head()
    )


def test_history_is_linear(database_url: str) -> None:
    """A branched history is almost always an accidental merge, and it makes
    `upgrade head` ambiguous."""
    script = ScriptDirectory.from_config(_config(database_url))

    assert len(script.get_heads()) == 1


def test_fixture_database_starts_at_head(database_url: str) -> None:
    script = ScriptDirectory.from_config(_config(database_url))

    assert asyncio.run(_current_version(database_url)) == script.get_current_head()
