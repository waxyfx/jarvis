"""Fixtures for cross-component end-to-end tests.

These run against a real uvicorn process on a real socket, with its own
database, so nothing here shares state with the per-package suites. That
matters: the behaviours under test — connection teardown, reconnection, TLS-less
loopback transport — are exactly the ones an in-process test client papers over.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "packages" / "atlas-backend"

E2E_DATABASE_URL = os.getenv("ATLAS_E2E_DATABASE_URL", "")
E2E_JWT_SECRET = "e2e-jwt-secret-that-is-long-enough-for-prod"
E2E_BOOTSTRAP_TOKEN = "e2e-bootstrap-token"

requires_e2e_db = pytest.mark.skipif(
    not E2E_DATABASE_URL,
    reason="set ATLAS_E2E_DATABASE_URL to run end-to-end tests",
)

_MANAGED_TABLES = (
    "audit_log",
    "device_sessions",
    "auth_challenges",
    "pairing_codes",
    "devices",
    "users",
)


def backend_settings():  # type: ignore[no-untyped-def]
    from atlas_backend.config import Settings

    return Settings(
        environment="dev",
        log_level="WARNING",
        database_url=E2E_DATABASE_URL,
        database_use_null_pool=True,
        jwt_secret=E2E_JWT_SECRET,  # type: ignore[arg-type]
        bootstrap_token=E2E_BOOTSTRAP_TOKEN,  # type: ignore[arg-type]
        owner_display_name="E2E Owner",
        heartbeat_interval_s=2.0,
        hello_timeout_s=5.0,
    )


async def _execute(statement: str, **params: object) -> list[tuple[object, ...]]:
    engine = create_async_engine(E2E_DATABASE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(statement), params)
            return [tuple(row) for row in result.all()] if result.returns_rows else []
    finally:
        await engine.dispose()


async def query(statement: str, **params: object) -> list[tuple[object, ...]]:
    return await _execute(statement, **params)


async def wait_for(
    statement: str, *, timeout_s: float = 10.0, **params: object
) -> list[tuple[object, ...]]:
    """Poll until the statement returns rows. Server-side teardown is async."""
    deadline = time.monotonic() + timeout_s
    while True:
        rows = await _execute(statement, **params)
        if rows or time.monotonic() >= deadline:
            return rows
        await asyncio.sleep(0.1)


@pytest.fixture(scope="session", autouse=True)
def _migrated_e2e_schema() -> Iterator[None]:
    if not E2E_DATABASE_URL:
        yield
        return

    from alembic import command
    from alembic.config import Config

    from atlas_backend.config import get_settings

    os.environ["ATLAS_DATABASE_URL"] = E2E_DATABASE_URL
    os.environ["ATLAS_JWT_SECRET"] = E2E_JWT_SECRET
    get_settings.cache_clear()

    asyncio.run(_execute("DROP SCHEMA public CASCADE"))
    asyncio.run(_execute("CREATE SCHEMA public"))
    command.upgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "head")
    yield


@pytest.fixture(autouse=True)
def _clean_e2e_tables(_migrated_e2e_schema: None) -> None:
    if not E2E_DATABASE_URL:
        return

    async def _truncate() -> None:
        await _execute("ALTER TABLE audit_log DISABLE TRIGGER USER")
        await _execute(f"TRUNCATE {', '.join(_MANAGED_TABLES)} RESTART IDENTITY CASCADE")
        await _execute("ALTER TABLE audit_log ENABLE TRIGGER USER")

    asyncio.run(_truncate())


@pytest.fixture
async def live_backend() -> AsyncIterator[str]:
    """A real backend on a real port. Yields its base URL."""
    from atlas_backend.main import create_app

    app = create_app(backend_settings())
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True, name="e2e-backend")
    thread.start()

    deadline = time.monotonic() + 20
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    if not server.started:
        server.should_exit = True
        raise RuntimeError("the end-to-end backend did not start")

    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        for _ in range(200):
            if not thread.is_alive():
                break
            await asyncio.sleep(0.05)
