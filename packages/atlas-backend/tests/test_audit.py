"""The audit chain: append-only in the database, verifiable end to end."""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy
from starlette.testclient import TestClient

from tests.conftest import (
    TEST_DATABASE_URL,
    authenticate,
    fetch_sql,
    pair_device,
    requires_db,
    run_sql,
    without_audit_triggers,
)

pytestmark = [requires_db, pytest.mark.integration]


def event_types() -> list[str]:
    return [row[0] for row in fetch_sql("SELECT event_type FROM audit_log ORDER BY seq")]


def verify(client: TestClient, token: str) -> dict[str, object]:
    response = client.post("/v1/audit/verify", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    return dict(response.json())


class TestRecording:
    def test_pairing_and_auth_are_recorded(self, client: TestClient) -> None:
        device = pair_device(client)
        authenticate(client, device)

        assert event_types() == [
            "pairing.started",
            "pairing.completed",
            "auth.challenge_issued",
            "auth.token_issued",
        ]

    def test_failed_pairing_is_recorded(self, client: TestClient) -> None:
        client.post(
            "/v1/pair/complete",
            json={
                "code": "ZZZZZZZZ",
                "public_key": "A" * 43,
                "signature": "A" * 86,
            },
        )
        assert "pairing.failed" in event_types()

    def test_revocation_is_recorded(self, client: TestClient) -> None:
        device = pair_device(client)
        token = authenticate(client, device)
        client.post(
            f"/v1/devices/{device.device_id}/revoke",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert "device.revoked" in event_types()

    def test_entries_are_readable_through_the_api(self, client: TestClient) -> None:
        device = pair_device(client)
        token = authenticate(client, device)

        response = client.get("/v1/audit", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        entries = response.json()
        assert entries[0]["seq"] > entries[-1]["seq"]  # newest first
        assert {"seq", "chain_index", "ts", "actor", "event_type"} <= set(entries[0])

    def test_audit_requires_authentication(self, client: TestClient) -> None:
        pair_device(client)
        assert client.get("/v1/audit").status_code == 401


class TestChainIntegrity:
    def test_chain_verifies_after_normal_activity(self, client: TestClient) -> None:
        device = pair_device(client)
        token = authenticate(client, device)

        result = verify(client, token)
        assert result["ok"] is True
        assert int(result["entries_checked"]) >= 4

    def test_chain_index_is_contiguous(self, client: TestClient) -> None:
        device = pair_device(client)
        authenticate(client, device)

        indices = [row[0] for row in fetch_sql("SELECT chain_index FROM audit_log ORDER BY seq")]
        assert indices == list(range(len(indices)))

    def test_tampering_with_content_is_detected(self, client: TestClient) -> None:
        device = pair_device(client)
        token = authenticate(client, device)
        assert verify(client, token)["ok"] is True

        without_audit_triggers(
            "UPDATE audit_log SET event_type = 'totally.innocent' WHERE chain_index = 1"
        )

        result = verify(client, token)
        assert result["ok"] is False
        assert result["reason"] == "content does not match the stored hash"

    def test_removing_an_entry_is_detected(self, client: TestClient) -> None:
        device = pair_device(client)
        token = authenticate(client, device)

        without_audit_triggers("DELETE FROM audit_log WHERE chain_index = 1")

        result = verify(client, token)
        assert result["ok"] is False
        assert "chain_index" in str(result["reason"])

    def test_rewriting_a_payload_is_detected(self, client: TestClient) -> None:
        device = pair_device(client)
        token = authenticate(client, device)

        without_audit_triggers(
            """UPDATE audit_log
               SET payload = jsonb_set(payload, '{name}', '"something else"')
               WHERE chain_index = 0"""
        )
        assert verify(client, token)["ok"] is False


class TestDatabaseLevelImmutability:
    """The triggers must hold even against direct SQL."""

    def test_update_is_refused(self, client: TestClient) -> None:
        pair_device(client)
        with pytest.raises(sqlalchemy.exc.DBAPIError, match="append-only"):
            run_sql("UPDATE audit_log SET actor = 'nobody' WHERE chain_index = 0")

    def test_delete_is_refused(self, client: TestClient) -> None:
        pair_device(client)
        with pytest.raises(sqlalchemy.exc.DBAPIError, match="append-only"):
            run_sql("DELETE FROM audit_log WHERE chain_index = 0")

    def test_truncate_is_refused(self, client: TestClient) -> None:
        pair_device(client)
        with pytest.raises(sqlalchemy.exc.DBAPIError, match="append-only"):
            run_sql("TRUNCATE audit_log")

    def test_insert_is_still_allowed(self, client: TestClient) -> None:
        device = pair_device(client)
        token = authenticate(client, device)
        before = len(event_types())

        client.get("/v1/devices", headers={"Authorization": f"Bearer {token}"})
        client.post("/v1/auth/challenge", json={"device_id": device.device_id})

        assert len(event_types()) > before


class TestConcurrentAppends:
    def test_parallel_writers_produce_one_valid_chain(self, client: TestClient) -> None:
        """The advisory lock is what stops two writers forking the chain."""
        device = pair_device(client)
        token = authenticate(client, device)

        asyncio.run(_hammer_challenges(device.device_id, count=25))

        result = verify(client, token)
        assert result["ok"] is True

        indices = [row[0] for row in fetch_sql("SELECT chain_index FROM audit_log ORDER BY seq")]
        assert indices == list(range(len(indices)))
        assert len(indices) >= 25


async def _hammer_challenges(device_id: str, *, count: int) -> None:
    """Drive many concurrent audit appends through the real service layer."""
    import uuid as uuid_module

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from atlas_backend.audit import AuditActor, AuditEvent, append

    engine = create_async_engine(TEST_DATABASE_URL, pool_size=10, max_overflow=10)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def one(index: int) -> None:
        async with factory() as session:
            await append(
                session,
                actor=AuditActor.SYSTEM,
                event_type=AuditEvent.CONNECTION_OPENED,
                device_id=uuid_module.UUID(device_id),
                payload={"n": index},
            )
            await session.commit()

    try:
        await asyncio.gather(*(one(index) for index in range(count)))
    finally:
        await engine.dispose()
