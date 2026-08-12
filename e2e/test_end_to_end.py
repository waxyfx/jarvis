"""M1 acceptance test.

The definition of done for M1 is: *the Windows agent connects to the backend
over a real socket, registers itself, and the audit log records it.* This file
asserts exactly that, using the production code paths on both sides — the real
``IdentityStore``, the real ``BackendClient``, the real ``AgentTransport``, and
a real uvicorn server. Nothing is stubbed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from atlas_agent.backend import BackendClient, EnrolmentRefusedError
from atlas_agent.config import AgentSettings
from atlas_agent.identity import IdentityStore
from atlas_agent.transport import AgentTransport, FatalTransportError
from e2e.conftest import E2E_BOOTSTRAP_TOKEN, query, requires_e2e_db, wait_for

pytestmark = [requires_e2e_db, pytest.mark.integration]


def agent_settings(base_url: str, tmp_path: Path, **overrides: object) -> AgentSettings:
    base: dict[str, object] = {
        "backend_url": base_url,
        "identity_path": tmp_path / "identity.json",
        # On Windows the key is DPAPI-protected regardless; this only matters
        # if the suite is ever run on another platform.
        "allow_plaintext_key": True,
        "device_name": "e2e-agent",
        "request_timeout_s": 10.0,
        "reconnect_initial_s": 0.2,
        "reconnect_max_s": 2.0,
    }
    return AgentSettings(**(base | overrides))  # type: ignore[arg-type]


async def issue_pairing_code(base_url: str, *, name: str = "e2e-agent") -> str:
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        response = await client.post(
            "/v1/pair/start",
            json={"kind": "windows_agent", "name": name},
            headers={"X-Atlas-Bootstrap-Token": E2E_BOOTSTRAP_TOKEN},
        )
    assert response.status_code == 201, response.text
    return str(response.json()["code"])


async def enrol_agent(base_url: str, tmp_path: Path) -> tuple[AgentSettings, IdentityStore]:
    settings = agent_settings(base_url, tmp_path)
    store = IdentityStore(settings.identity_path, allow_plaintext=True)

    code = await issue_pairing_code(base_url)
    identity = await BackendClient(settings).enrol(store.create(), code)
    store.save(identity)
    return settings, store


class TestAcceptance:
    async def test_agent_pairs_connects_and_is_audited(
        self, live_backend: str, tmp_path: Path
    ) -> None:
        settings, store = await enrol_agent(live_backend, tmp_path)

        identity = store.load()
        assert identity is not None and identity.is_enrolled

        transport = AgentTransport(settings, identity, capabilities=("system", "apps"))
        stop = asyncio.Event()
        runner = asyncio.create_task(transport.run(stop=stop))

        try:
            await asyncio.wait_for(transport.connected.wait(), timeout=15)

            opened = await wait_for(
                "SELECT payload FROM audit_log WHERE event_type = 'connection.opened'"
            )
            assert opened

            sessions = await wait_for(
                "SELECT handshake_ok FROM device_sessions WHERE handshake_ok IS TRUE"
            )
            assert sessions == [(True,)]
        finally:
            stop.set()
            await asyncio.wait_for(runner, timeout=15)

        # The close path runs to completion against a real server, which the
        # in-process test client cannot demonstrate.
        closed = await wait_for(
            "SELECT payload FROM audit_log WHERE event_type = 'connection.closed'"
        )
        assert closed

        ended = await wait_for(
            "SELECT close_reason FROM device_sessions WHERE ended_at IS NOT NULL"
        )
        assert ended

    async def test_audit_chain_is_intact_after_a_full_session(
        self, live_backend: str, tmp_path: Path
    ) -> None:
        settings, store = await enrol_agent(live_backend, tmp_path)
        identity = store.load()
        assert identity is not None

        transport = AgentTransport(settings, identity)
        stop = asyncio.Event()
        runner = asyncio.create_task(transport.run(stop=stop))
        await asyncio.wait_for(transport.connected.wait(), timeout=15)
        stop.set()
        await asyncio.wait_for(runner, timeout=15)

        await wait_for("SELECT seq FROM audit_log WHERE event_type = 'connection.closed'")

        rows = await query("SELECT chain_index FROM audit_log ORDER BY seq")
        assert [row[0] for row in rows] == list(range(len(rows)))
        assert len(rows) >= 6  # pairing, auth, connection open/close


class TestIdentityLifecycle:
    async def test_pairing_code_cannot_be_reused(self, live_backend: str, tmp_path: Path) -> None:
        settings = agent_settings(live_backend, tmp_path)
        store = IdentityStore(settings.identity_path, allow_plaintext=True)
        code = await issue_pairing_code(live_backend)

        client = BackendClient(settings)
        await client.enrol(store.create(), code)

        with pytest.raises(EnrolmentRefusedError):
            await client.enrol(store.create(), code)

    async def test_identity_survives_a_restart(self, live_backend: str, tmp_path: Path) -> None:
        settings, store = await enrol_agent(live_backend, tmp_path)
        first = store.load()
        assert first is not None

        # A fresh store object, as a restarted process would have.
        reopened = IdentityStore(settings.identity_path, allow_plaintext=True)
        second = reopened.load()
        assert second is not None
        assert second.device_id == first.device_id
        assert second.private_key == first.private_key

        transport = AgentTransport(settings, second)
        stop = asyncio.Event()
        runner = asyncio.create_task(transport.run(stop=stop))
        try:
            await asyncio.wait_for(transport.connected.wait(), timeout=15)
        finally:
            stop.set()
            await asyncio.wait_for(runner, timeout=15)


class TestFailureHandling:
    async def test_revoked_device_stops_the_agent(self, live_backend: str, tmp_path: Path) -> None:
        settings, store = await enrol_agent(live_backend, tmp_path)
        identity = store.load()
        assert identity is not None

        transport = AgentTransport(settings, identity)
        stop = asyncio.Event()
        runner = asyncio.create_task(transport.run(stop=stop))
        await asyncio.wait_for(transport.connected.wait(), timeout=15)

        token = await BackendClient(settings).authenticate(identity)
        async with httpx.AsyncClient(base_url=live_backend, timeout=10.0) as client:
            response = await client.post(
                f"/v1/devices/{identity.device_id}/revoke",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200

        # Retrying forever would hide the revocation; the agent must stop and
        # say why.
        with pytest.raises(FatalTransportError, match="revoked"):
            await asyncio.wait_for(runner, timeout=20)

    async def test_unpaired_agent_cannot_authenticate(
        self, live_backend: str, tmp_path: Path
    ) -> None:
        from atlas_agent.backend import BackendError

        settings = agent_settings(live_backend, tmp_path)
        store = IdentityStore(settings.identity_path, allow_plaintext=True)

        with pytest.raises(BackendError, match="not paired"):
            await BackendClient(settings).authenticate(store.create())

    async def test_agent_reports_backend_unreachable(self, tmp_path: Path) -> None:
        # Port 1 is reserved and nothing listens there.
        settings = agent_settings("http://127.0.0.1:1", tmp_path, request_timeout_s=2.0)
        store = IdentityStore(settings.identity_path, allow_plaintext=True)

        from atlas_agent.backend import BackendError

        with pytest.raises(BackendError, match="could not reach the backend"):
            await BackendClient(settings).enrol(store.create(), "ABCDEFGH")


class TestHeartbeat:
    async def test_connection_survives_past_the_heartbeat_deadline(
        self, live_backend: str, tmp_path: Path
    ) -> None:
        """The agent must answer server pings, or it is dropped as dead.

        The server's interval is 2s with 2 grace periods, so staying up for 6s
        proves the pong path works end to end.
        """
        settings, store = await enrol_agent(live_backend, tmp_path)
        identity = store.load()
        assert identity is not None

        transport = AgentTransport(settings, identity)
        stop = asyncio.Event()
        runner = asyncio.create_task(transport.run(stop=stop))
        try:
            await asyncio.wait_for(transport.connected.wait(), timeout=15)
            await asyncio.sleep(6)
            assert transport.connected.is_set()
            assert not runner.done()

            reconnects = await query(
                "SELECT count(*) FROM audit_log WHERE event_type = 'connection.opened'"
            )
            assert reconnects == [(1,)]
        finally:
            stop.set()
            await asyncio.wait_for(runner, timeout=15)
