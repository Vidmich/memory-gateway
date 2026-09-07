"""Alembic environment, wired for async SQLAlchemy.

The database URL always comes from ``Settings`` so migrations cannot drift onto a
different database than the application.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db.base import Base

# Importing the models package registers every table on Base.metadata; without it
# autogenerate silently produces empty revisions.
import app.db.models  # noqa: F401  isort:skip

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# An explicit url (set by tooling, e.g. the test-database fixture) wins; otherwise the
# application's own configured database is used.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # Required for the partitioned log tables in task 07: they are created with raw
        # DDL and must not be seen as drift.
        include_schemas=False,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
