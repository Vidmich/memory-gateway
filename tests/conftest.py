"""Shared fixtures.

Database-backed tests are marked ``db``. They build their schema by running the
**migrations** rather than ``metadata.create_all``, so a broken or missing migration
fails the suite instead of passing against a schema no deployment will ever have.

When no PostgreSQL is reachable the ``db`` fixtures skip rather than fail: the rest of
the suite still runs on a laptop without the stack up, and CI always has the service
container.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine

from app.core.clients import Clients
from app.core.config import Settings, get_settings
from app.main import create_app

TEST_DB_SUFFIX = "_pytest"


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    """A fully wired app instance with its lifespan run.

    Creating the clients opens no sockets, so this works with the stack down; only the
    probes themselves need live services.
    """
    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


@pytest.fixture
def clients(app: FastAPI) -> Clients:
    result: Clients = app.state.clients
    return result


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------


def _with_database(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


async def _recreate_database(admin_url: str, database: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
            await connection.execute(text(f'CREATE DATABASE "{database}"'))
    finally:
        await engine.dispose()


async def _drop_database(admin_url: str, database: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def database_url(settings_for_session: Settings) -> Iterator[str]:
    """A throwaway database for the whole test session, built from the migrations."""
    base_url = settings_for_session.database_url
    source_name = urlsplit(base_url).path.lstrip("/")
    test_name = f"{source_name}{TEST_DB_SUFFIX}"

    admin_url = _with_database(base_url, "postgres")
    test_url = _with_database(base_url, test_name)

    try:
        asyncio.run(_recreate_database(admin_url, test_name))
    except Exception as exc:  # no server, wrong credentials, no CREATE DATABASE right
        if os.getenv("REQUIRE_DB_TESTS") == "1":
            raise
        pytest.skip(f"PostgreSQL not available for db tests: {exc}")

    alembic_config = AlembicConfig("alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", test_url)
    command.upgrade(alembic_config, "head")

    yield test_url

    asyncio.run(_drop_database(admin_url, test_name))


@pytest.fixture(scope="session")
def settings_for_session() -> Settings:
    return get_settings()


@pytest.fixture
async def db_connection(database_url: str) -> AsyncIterator[AsyncConnection]:
    """An open transaction rolled back after the test, so tests never see each other's
    rows and no cleanup code is needed."""
    engine = create_async_engine(database_url, poolclass=None)
    connection = await engine.connect()
    transaction = await connection.begin()
    try:
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


@pytest.fixture
async def db_session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(bind=db_connection, expire_on_commit=False) as session:
        yield session


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class FakeAsync:
    """Callable that records its calls and either returns or raises."""

    def __init__(self, *, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result
