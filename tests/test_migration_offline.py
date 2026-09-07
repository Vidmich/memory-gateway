"""Guard against the model/migration drift that only shows up on deploy.

Alembic can render a migration to SQL without a database ("offline mode"), which makes
these checks runnable anywhere. They do not replace the round-trip in
``test_migrations.py`` — that one needs a real server — but they do catch the common
failure: a column added to a model and forgotten in the revision, which passes every test
that builds its schema from ``metadata`` and then breaks the first deployment.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Table

from app.db.base import Base

# Importing the models package is what puts the tables on the metadata.
import app.db.models  # noqa: F401  isort:skip


@pytest.fixture(scope="module")
def upgrade_sql() -> str:
    return _render(command.upgrade, "head")


def _tables() -> list[Table]:
    return list(Base.metadata.sorted_tables)


def test_the_migration_renders(upgrade_sql: str) -> None:
    assert "CREATE TABLE" in upgrade_sql


def test_models_declare_at_least_one_table() -> None:
    """Otherwise every assertion below would pass vacuously."""
    assert len(_tables()) >= 5


@pytest.mark.parametrize("table", _tables(), ids=lambda table: str(table.name))
def test_every_mapped_table_is_created_by_a_migration(table: Table, upgrade_sql: str) -> None:
    assert f"CREATE TABLE {table.name}" in upgrade_sql


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [(table.name, column.name) for table in _tables() for column in table.columns],
    ids=lambda value: str(value),
)
def test_every_mapped_column_is_created_by_a_migration(
    table_name: str, column_name: str, upgrade_sql: str
) -> None:
    statement = _create_statement(upgrade_sql, table_name)

    assert f"{column_name} " in statement, f"{table_name}.{column_name} is missing"


def test_downgrade_renders_too() -> None:
    """A revision that cannot be rolled back is discovered at the worst possible moment."""
    assert "DROP TABLE" in _render(command.downgrade, "head:base")


@pytest.mark.parametrize(
    "constraint_name",
    # `constraint.name` is already expanded by the naming convention here, which is
    # exactly the name the migration has to emit.
    sorted(
        str(constraint.name)
        for table in _tables()
        for constraint in table.constraints
        if constraint.name is not None
    ),
    ids=str,
)
def test_constraint_names_match_the_models(constraint_name: str, upgrade_sql: str) -> None:
    """Names drift silently: spelling a check constraint's full name in the migration
    makes the naming convention expand it twice, and a later `DROP CONSTRAINT` misses."""
    assert f"CONSTRAINT {constraint_name} " in upgrade_sql


def _render(action: Any, revision: str) -> str:
    buffer = io.StringIO()
    # `output_buffer`, not `stdout`: offline mode writes the migration SQL there.
    config = AlembicConfig("alembic.ini", output_buffer=buffer)
    config.set_main_option("sqlalchemy.url", "postgresql+asyncpg://u:p@localhost/db")
    action(config, revision, sql=True)
    return buffer.getvalue()


def _create_statement(sql: str, table_name: str) -> str:
    marker = f"CREATE TABLE {table_name} ("
    start = sql.index(marker)
    return sql[start : sql.index(");", start)]
