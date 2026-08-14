"""Shared fixtures.

Integration tests need a real PostgreSQL, because the things worth testing here
— advisory locks, append-only triggers, ``FOR UPDATE`` — do not exist in a
substitute. They are skipped, loudly, when ``ATLAS_TEST_DATABASE_URL`` is unset,
rather than being silently replaced with a weaker check.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

from atlas_shared.auth import challenge_signing_input, pairing_signing_input
from atlas_shared.crypto import b64u_decode, b64u_encode, generate_keypair, sign
from atlas_shared.enums import DeviceKind

BACKEND_ROOT = Path(__file__).resolve().parents[1]

TEST_DATABASE_URL = os.getenv("ATLAS_TEST_DATABASE_URL", "")
TEST_JWT_SECRET = "test-jwt-secret-that-is-long-enough-for-prod-rules"
TEST_BOOTSTRAP_TOKEN = "test-bootstrap-token"
#: Fixed so every test run pins the same server identity.
TEST_SERVER_SIGNING_KEY = b64u_encode(bytes(range(32)))

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set ATLAS_TEST_DATABASE_URL to run integration tests",
)

_MANAGED_TABLES = (
    "messages",
    "conversations",
    "api_usage",
    "tool_calls",
    "permissions",
    "activity_samples",
    "system_telemetry",
    "audit_log",
    "device_sessions",
    "auth_challenges",
    "pairing_codes",
    "devices",
    "users",
)


def _make_settings():  # type: ignore[no-untyped-def]
    from atlas_backend.config import Settings

    return Settings(
        environment="dev",
        log_level="WARNING",
        database_url=TEST_DATABASE_URL or "postgresql+asyncpg://unused/unused",
        jwt_secret=TEST_JWT_SECRET,  # type: ignore[arg-type]
        server_signing_key=TEST_SERVER_SIGNING_KEY,  # type: ignore[arg-type]
        bootstrap_token=TEST_BOOTSTRAP_TOKEN,  # type: ignore[arg-type]
        owner_display_name="Test Owner",
        database_use_null_pool=True,
        # The server's path check is textual, so this root need not exist. The
        # agent resolves paths for real and has the last word.
        allowed_file_roots=("C:/atlas-test-root",),
        heartbeat_interval_s=2.0,
        hello_timeout_s=2.0,
        pairing_rate_limit_per_minute=100,
    )


@pytest.fixture(scope="session")
def settings():  # type: ignore[no-untyped-def]
    return _make_settings()


async def _reset_schema(url: str) -> None:
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


async def _truncate(url: str) -> None:
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            # The append-only triggers block TRUNCATE by design. Lifting them
            # here is the fixture's privilege, not the application's: no code
            # path in atlas_backend can do this.
            await connection.execute(text("ALTER TABLE audit_log DISABLE TRIGGER USER"))
            await connection.execute(
                text(f"TRUNCATE {', '.join(_MANAGED_TABLES)} RESTART IDENTITY CASCADE")
            )
            await connection.execute(text("ALTER TABLE audit_log ENABLE TRIGGER USER"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _migrated_schema() -> Iterator[None]:
    if not TEST_DATABASE_URL:
        yield
        return

    from alembic import command
    from alembic.config import Config

    from atlas_backend.config import get_settings

    os.environ["ATLAS_DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["ATLAS_JWT_SECRET"] = TEST_JWT_SECRET
    os.environ["ATLAS_SERVER_SIGNING_KEY"] = TEST_SERVER_SIGNING_KEY
    get_settings.cache_clear()

    asyncio.run(_reset_schema(TEST_DATABASE_URL))
    command.upgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "head")
    yield


@pytest.fixture(autouse=True)
def _clean_tables(_migrated_schema: None) -> None:
    if TEST_DATABASE_URL:
        asyncio.run(_truncate(TEST_DATABASE_URL))


@pytest.fixture
def app(settings):  # type: ignore[no-untyped-def]
    from atlas_backend.main import create_app

    return create_app(settings)


@pytest.fixture
def client(app) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    with TestClient(app) as test_client:
        yield test_client


def run_sql(statement: str, **params: object) -> None:
    """Execute raw SQL against the test database, outside the application."""

    async def _go() -> None:
        engine = create_async_engine(
            TEST_DATABASE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool
        )
        try:
            async with engine.connect() as connection:
                await connection.execute(text(statement), params)
        finally:
            await engine.dispose()

    asyncio.run(_go())


def fetch_sql(statement: str, **params: object) -> list[tuple[object, ...]]:
    async def _go() -> list[tuple[object, ...]]:
        engine = create_async_engine(
            TEST_DATABASE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool
        )
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text(statement), params)
                return [tuple(row) for row in result.all()]
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def wait_for_sql(
    statement: str, *, timeout_s: float = 5.0, **params: object
) -> list[tuple[object, ...]]:
    """Poll until the query returns rows, or give up.

    Connection teardown on the server happens after the client's socket context
    exits, so anything written in a handler's ``finally`` needs to be waited
    for rather than assumed.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        rows = fetch_sql(statement, **params)
        if rows or time.monotonic() >= deadline:
            return rows
        time.sleep(0.05)


def without_audit_triggers(statement: str, **params: object) -> None:
    """Run a statement the append-only triggers would otherwise refuse.

    Used only to *simulate tampering* so the detection code can be tested.
    """
    run_sql("ALTER TABLE audit_log DISABLE TRIGGER USER")
    try:
        run_sql(statement, **params)
    finally:
        run_sql("ALTER TABLE audit_log ENABLE TRIGGER USER")


# --------------------------------------------------------------- helpers


@dataclass(frozen=True, slots=True)
class PairedDevice:
    device_id: str
    kind: DeviceKind
    private_key: bytes
    public_key: bytes

    def sign_challenge(self, nonce_b64: str) -> str:
        message = challenge_signing_input(self.device_id, b64u_decode(nonce_b64))
        return b64u_encode(sign(self.private_key, message))


def start_pairing(
    client: TestClient,
    *,
    kind: DeviceKind = DeviceKind.WINDOWS_AGENT,
    name: str = "workstation",
    bearer: str | None = None,
) -> dict[str, str]:
    headers = (
        {"Authorization": f"Bearer {bearer}"}
        if bearer
        else {"X-Atlas-Bootstrap-Token": TEST_BOOTSTRAP_TOKEN}
    )
    response = client.post(
        "/v1/pair/start",
        json={"kind": kind.value, "name": name},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def pair_device(
    client: TestClient,
    *,
    kind: DeviceKind = DeviceKind.WINDOWS_AGENT,
    name: str = "workstation",
    bearer: str | None = None,
) -> PairedDevice:
    """Run the full enrolment handshake and return the new device's identity."""
    started = start_pairing(client, kind=kind, name=name, bearer=bearer)
    private_key, public_key = generate_keypair()
    proof = sign(private_key, pairing_signing_input(started["code"], public_key))

    response = client.post(
        "/v1/pair/complete",
        json={
            "code": started["code"],
            "public_key": b64u_encode(public_key),
            "signature": b64u_encode(proof),
        },
    )
    assert response.status_code == 201, response.text
    return PairedDevice(
        device_id=response.json()["device_id"],
        kind=kind,
        private_key=private_key,
        public_key=public_key,
    )


def authenticate(client: TestClient, device: PairedDevice) -> str:
    """Complete challenge/response and return a bearer token."""
    challenge = client.post("/v1/auth/challenge", json={"device_id": device.device_id})
    assert challenge.status_code == 200, challenge.text
    nonce = challenge.json()["nonce"]

    token = client.post(
        "/v1/auth/token",
        json={
            "device_id": device.device_id,
            "nonce": nonce,
            "signature": device.sign_challenge(nonce),
        },
    )
    assert token.status_code == 200, token.text
    return str(token.json()["access_token"])


def paired_and_authenticated(
    client: TestClient,
    *,
    kind: DeviceKind = DeviceKind.WINDOWS_AGENT,
    name: str = "workstation",
    bearer: str | None = None,
) -> tuple[PairedDevice, str]:
    device = pair_device(client, kind=kind, name=name, bearer=bearer)
    return device, authenticate(client, device)
