"""M2 regression suite: how the system behaves when things go wrong.

One test per failure mode agreed for the M2 checkpoint. These craft traffic the
normal API cannot produce — replayed commands, stale timestamps, forged
signatures — by reaching into the in-process app for the hub and the server's
signing key. Everything the *agent* does in response is production code.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from atlas_agent.backend import BackendClient
from atlas_agent.config import AgentSettings
from atlas_agent.identity import IdentityStore
from atlas_agent.runner import ToolRunner
from atlas_agent.safety.mode import ModeChangeSource, SafeModeController
from atlas_agent.safety.paths import PathGuard
from atlas_agent.transport import AgentTransport
from atlas_shared.crypto import generate_keypair
from atlas_shared.enums import AgentMode, RefusalReason, RiskLevel, ToolStatus
from atlas_shared.ids import new_ulid
from atlas_shared.protocol.envelope import Envelope, sign_envelope
from atlas_shared.protocol.messages import ToolExecute, build_envelope
from atlas_shared.tools.manifest import RiskContext
from e2e.conftest import E2E_BOOTSTRAP_TOKEN, query, requires_e2e_db, wait_for

pytestmark = [requires_e2e_db, pytest.mark.integration]


class Fixture:
    """A paired, connected agent plus the handles needed to poke at it."""

    def __init__(
        self,
        *,
        settings: AgentSettings,
        store: IdentityStore,
        controller: SafeModeController,
        transport: AgentTransport,
        app: Any,
        workspace: Path,
        stop: asyncio.Event,
        task: asyncio.Task[None],
    ) -> None:
        self.settings = settings
        self.store = store
        self.controller = controller
        self.transport = transport
        self.app = app
        self.workspace = workspace
        self.stop = stop
        self.task = task

    @property
    def device_id(self):  # type: ignore[no-untyped-def]
        import uuid as uuid_module

        identity = self.store.load()
        assert identity is not None and identity.device_id
        return uuid_module.UUID(identity.device_id)

    def command_envelope(
        self,
        tool: str = "system.metrics",
        args: dict[str, Any] | None = None,
        *,
        risk: RiskLevel = RiskLevel.LOW,
        ts: datetime | None = None,
    ) -> Envelope:
        envelope = build_envelope(
            "agent.tool.execute",
            ToolExecute(
                call_id=new_ulid(),
                tool=tool,
                tool_version=1,
                args=args or {},
                risk=risk,
                deadline_s=20.0,
            ),
            corr_id=new_ulid(),
        )
        if ts is not None:
            envelope = envelope.model_copy(update={"ts": ts})
        return envelope

    def sign(self, envelope: Envelope) -> Envelope:
        return self.app.state.server_identity.sign(envelope)

    async def deliver(self, envelope: Envelope, *, timeout_s: float = 15.0):  # type: ignore[no-untyped-def]
        return await self.app.state.hub.request(self.device_id, envelope, timeout_s=timeout_s)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "notes.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "forbidden").mkdir()
    (tmp_path / "forbidden" / "secret.txt").write_text("nope", encoding="utf-8")
    return tmp_path


@pytest.fixture
def allowed_file_roots(workspace: Path) -> tuple[str, ...]:
    return (str(workspace / "allowed"),)


async def build_agent(
    live_backend: str, tmp_path: Path, workspace: Path, backend_app: Any
) -> Fixture:
    settings = AgentSettings(
        backend_url=live_backend,
        identity_path=tmp_path / "identity.json",
        mode_state_path=tmp_path / "mode.json",
        allow_plaintext_key=True,
        device_name="resilience-agent",
        request_timeout_s=10.0,
        reconnect_initial_s=0.2,
        reconnect_max_s=1.0,
        monitor_enabled=False,
    )
    store = IdentityStore(settings.identity_path, allow_plaintext=True)

    async with httpx.AsyncClient(base_url=live_backend, timeout=15.0) as client:
        started = await client.post(
            "/v1/pair/start",
            json={"kind": "windows_agent", "name": "resilience-agent"},
            headers={"X-Atlas-Bootstrap-Token": E2E_BOOTSTRAP_TOKEN},
        )
    assert started.status_code == 201

    identity = await BackendClient(settings).enrol(store.create(), started.json()["code"])
    store.save(identity)

    allowed = workspace / "allowed"
    controller = SafeModeController(settings.mode_state_path)
    transport = AgentTransport(
        settings,
        identity,
        runner=ToolRunner(
            safe_mode=controller,
            path_guard=PathGuard([allowed]),
            risk_context=RiskContext(
                allowed_roots=(str(allowed),), executable_roots=(r"C:\Windows",)
            ),
        ),
        safe_mode=controller,
    )

    stop = asyncio.Event()
    task = asyncio.create_task(transport.run(stop=stop))
    await asyncio.wait_for(transport.connected.wait(), timeout=20)

    return Fixture(
        settings=settings,
        store=store,
        controller=controller,
        transport=transport,
        app=backend_app,
        workspace=workspace,
        stop=stop,
        task=task,
    )


@pytest.fixture
async def agent(live_backend: str, tmp_path: Path, workspace: Path, backend_app: Any):  # type: ignore[no-untyped-def]
    fixture = await build_agent(live_backend, tmp_path, workspace, backend_app)
    try:
        yield fixture
    finally:
        fixture.stop.set()
        await asyncio.wait_for(fixture.task, timeout=20)


# ---------------------------------------------------------------------------
# 1. Loss of connection to the backend
# ---------------------------------------------------------------------------


class TestConnectionLoss:
    async def test_agent_reconnects_after_being_dropped(self, agent: Fixture) -> None:
        await agent.app.state.hub.disconnect(agent.device_id, reason="test")
        agent.transport.connected.clear()

        await asyncio.wait_for(agent.transport.connected.wait(), timeout=25)

        opened = await query(
            "SELECT count(*) FROM audit_log WHERE event_type = 'connection.opened'"
        )
        assert opened[0][0] >= 2

    async def test_commands_work_again_after_reconnecting(self, agent: Fixture) -> None:
        await agent.app.state.hub.disconnect(agent.device_id, reason="test")
        agent.transport.connected.clear()
        await asyncio.wait_for(agent.transport.connected.wait(), timeout=25)

        result = await agent.deliver(agent.sign(agent.command_envelope()))
        assert result.status is ToolStatus.OK

    async def test_the_kill_switch_works_while_disconnected(self, agent: Fixture) -> None:
        await agent.app.state.hub.disconnect(agent.device_id, reason="test")

        # No network involved: the controller writes a local file. This is the
        # property that makes it a kill switch rather than a request.
        agent.controller.enter_safe_mode("offline test", ModeChangeSource.LOCAL_HOTKEY)
        assert agent.controller.is_safe is True
        assert SafeModeController(agent.settings.mode_state_path).is_safe is True


# ---------------------------------------------------------------------------
# 2. Agent restart
# ---------------------------------------------------------------------------


class TestRestart:
    async def test_identity_and_mode_survive_a_restart(self, agent: Fixture) -> None:
        agent.controller.enter_safe_mode("before restart", ModeChangeSource.LOCAL_CLI)
        first_device = agent.store.load()
        assert first_device is not None

        agent.stop.set()
        await asyncio.wait_for(agent.task, timeout=20)

        # A fresh process would build both of these from disk.
        reopened_store = IdentityStore(agent.settings.identity_path, allow_plaintext=True)
        reopened_mode = SafeModeController(agent.settings.mode_state_path)

        second_device = reopened_store.load()
        assert second_device is not None
        assert second_device.device_id == first_device.device_id
        assert second_device.server_public_key == first_device.server_public_key
        # An agent stopped for a reason stays stopped.
        assert reopened_mode.is_safe is True
        assert reopened_mode.current.reason == "before restart"


# ---------------------------------------------------------------------------
# 3. Corrupted SAFE MODE state
# ---------------------------------------------------------------------------


class TestCorruptedState:
    async def test_unreadable_state_fails_safe_and_blocks_commands(self, agent: Fixture) -> None:
        agent.settings.mode_state_path.write_text("{ this is not json", encoding="utf-8")

        recovered = SafeModeController(agent.settings.mode_state_path)
        assert recovered.mode is AgentMode.SAFE
        assert "unreadable" in recovered.current.reason

        # And the runner built on it refuses accordingly.
        runner = ToolRunner(
            safe_mode=recovered,
            path_guard=PathGuard([agent.workspace / "allowed"]),
            risk_context=RiskContext(allowed_roots=(str(agent.workspace / "allowed"),)),
        )
        result = await runner.run(
            ToolExecute(
                call_id=new_ulid(),
                tool="app.close",
                tool_version=1,
                args={"name": "nothing", "force": False},
                risk=RiskLevel.MEDIUM,
                deadline_s=10.0,
            )
        )
        assert result.status is ToolStatus.REFUSED
        assert result.refusal is RefusalReason.SAFE_MODE


# ---------------------------------------------------------------------------
# 4. The same signed command delivered twice
# ---------------------------------------------------------------------------


class TestReplay:
    async def test_a_replayed_command_runs_only_once(self, agent: Fixture) -> None:
        envelope = agent.sign(agent.command_envelope())

        first = await agent.deliver(envelope)
        assert first.status is ToolStatus.OK

        second = await agent.deliver(envelope)
        assert second.status is ToolStatus.REFUSED
        assert second.refusal is RefusalReason.REPLAYED

    async def test_replay_is_refused_across_a_reconnect(self, agent: Fixture) -> None:
        envelope = agent.sign(agent.command_envelope())
        assert (await agent.deliver(envelope)).status is ToolStatus.OK

        await agent.app.state.hub.disconnect(agent.device_id, reason="test")
        agent.transport.connected.clear()
        await asyncio.wait_for(agent.transport.connected.wait(), timeout=25)

        # The seen-id cache belongs to the agent, not to a connection, so a new
        # socket is not a fresh start for an attacker.
        replayed = await agent.deliver(envelope)
        assert replayed.refusal is RefusalReason.REPLAYED


# ---------------------------------------------------------------------------
# 5. An expired command
# ---------------------------------------------------------------------------


class TestExpiry:
    async def test_a_stale_command_is_refused(self, agent: Fixture) -> None:
        stale = agent.command_envelope(ts=datetime.now(UTC) - timedelta(hours=1))
        result = await agent.deliver(agent.sign(stale))

        assert result.status is ToolStatus.REFUSED
        assert result.refusal is RefusalReason.EXPIRED

    async def test_a_command_from_the_future_is_refused(self, agent: Fixture) -> None:
        ahead = agent.command_envelope(ts=datetime.now(UTC) + timedelta(hours=1))
        result = await agent.deliver(agent.sign(ahead))
        assert result.refusal is RefusalReason.EXPIRED

    async def test_a_fresh_command_is_accepted(self, agent: Fixture) -> None:
        fresh = agent.command_envelope(ts=datetime.now(UTC) - timedelta(seconds=5))
        assert (await agent.deliver(agent.sign(fresh))).status is ToolStatus.OK


# ---------------------------------------------------------------------------
# 6. An unknown tool
# ---------------------------------------------------------------------------


class TestUnknownTool:
    async def test_the_agent_refuses_a_tool_it_does_not_have(self, agent: Fixture) -> None:
        envelope = agent.sign(agent.command_envelope(tool="app.invented_by_the_model"))
        result = await agent.deliver(envelope)

        assert result.status is ToolStatus.REFUSED
        assert result.refusal is RefusalReason.UNKNOWN_TOOL

    async def test_the_api_rejects_it_before_dispatch(
        self, agent: Fixture, live_backend: str
    ) -> None:
        token = await BackendClient(agent.settings).authenticate(agent.store.load())  # type: ignore[arg-type]
        async with httpx.AsyncClient(base_url=live_backend, timeout=15.0) as client:
            response = await client.post(
                "/v1/tools/app.invented/execute",
                json={"args": {}},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 400
        assert "unknown tool" in response.json()["message"]


# ---------------------------------------------------------------------------
# 7. A command with a bad signature
# ---------------------------------------------------------------------------


class TestForgedCommand:
    async def test_a_command_signed_by_a_foreign_key_is_refused(self, agent: Fixture) -> None:
        attacker_private, _ = generate_keypair()
        forged = sign_envelope(agent.command_envelope(), attacker_private)

        result = await agent.deliver(forged)
        assert result.status is ToolStatus.REFUSED
        assert result.refusal is RefusalReason.SIGNATURE_INVALID

    async def test_an_unsigned_command_is_refused(self, agent: Fixture) -> None:
        result = await agent.deliver(agent.command_envelope())
        assert result.refusal is RefusalReason.SIGNATURE_INVALID

    async def test_a_forged_command_drives_the_agent_into_safe_mode(self, agent: Fixture) -> None:
        attacker_private, _ = generate_keypair()
        await agent.deliver(sign_envelope(agent.command_envelope(), attacker_private))

        # A command that fails verification is either a bug or an attack. Either
        # way the agent stops taking instructions.
        assert agent.controller.is_safe is True
        assert agent.controller.current.source is ModeChangeSource.AUTOMATIC

    async def test_tampering_with_a_signed_command_invalidates_it(self, agent: Fixture) -> None:
        signed = agent.sign(agent.command_envelope(tool="system.metrics"))
        tampered = signed.model_copy(update={"payload": {**signed.payload, "tool": "app.close"}})
        result = await agent.deliver(tampered)
        assert result.refusal is RefusalReason.SIGNATURE_INVALID


# ---------------------------------------------------------------------------
# 9. Path guard bypass via a junction
# ---------------------------------------------------------------------------


class TestPathGuardBypass:
    @pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
    async def test_a_junction_out_of_the_roots_is_refused_end_to_end(self, agent: Fixture) -> None:
        link = agent.workspace / "allowed" / "escape"
        created = subprocess.run(  # noqa: S603
            ["cmd", "/c", "mklink", "/J", str(link), str(agent.workspace / "forbidden")],  # noqa: S607
            capture_output=True,
            check=False,
        )
        if created.returncode != 0:
            pytest.skip("this system does not permit creating junctions")

        # The path looks like it is inside the allowed root. The server's textual
        # pre-filter agrees. Only the agent, resolving it for real, does not.
        envelope = agent.sign(
            agent.command_envelope("fs.search", {"query": "secret", "root": str(link)})
        )
        result = await agent.deliver(envelope)

        assert result.status is ToolStatus.REFUSED
        assert result.refusal is RefusalReason.PATH_OUTSIDE_ROOTS


# ---------------------------------------------------------------------------
# 10. The backend trying to release SAFE MODE
# ---------------------------------------------------------------------------


class TestRemoteCannotRelease:
    async def test_the_backend_can_engage_but_not_release(self, agent: Fixture) -> None:
        from atlas_shared.protocol.messages import EnterSafeMode

        engage = agent.sign(
            build_envelope(
                "agent.mode.enter_safe",
                EnterSafeMode(reason="requested by the server"),
                corr_id=new_ulid(),
            )
        )
        await agent.app.state.hub.send(agent.device_id, engage.to_json())
        await asyncio.sleep(0.5)
        assert agent.controller.is_safe is True

        # There is no message that leaves SAFE MODE, and the controller refuses
        # any non-local source outright.
        from atlas_agent.safety.mode import SafeModeViolationError

        with pytest.raises(SafeModeViolationError):
            agent.controller.leave_safe_mode(ModeChangeSource.REMOTE_REQUEST)
        with pytest.raises(SafeModeViolationError):
            agent.controller.leave_safe_mode(ModeChangeSource.AUTOMATIC)

        assert agent.controller.is_safe is True

    async def test_medium_risk_stays_blocked_until_released_locally(self, agent: Fixture) -> None:
        agent.controller.enter_safe_mode("server asked", ModeChangeSource.REMOTE_REQUEST)

        blocked = await agent.deliver(
            agent.sign(
                agent.command_envelope(
                    "app.close", {"name": "nothing", "force": False}, risk=RiskLevel.MEDIUM
                )
            )
        )
        assert blocked.refusal is RefusalReason.SAFE_MODE

        agent.controller.leave_safe_mode(ModeChangeSource.LOCAL_TRAY)
        allowed = await agent.deliver(
            agent.sign(
                agent.command_envelope(
                    "app.close", {"name": "nothing", "force": False}, risk=RiskLevel.MEDIUM
                )
            )
        )
        assert allowed.status is ToolStatus.ERROR  # ran, found no such process


class TestAuditOfFailures:
    async def test_refusals_are_recorded(self, agent: Fixture) -> None:
        attacker_private, _ = generate_keypair()
        await agent.deliver(sign_envelope(agent.command_envelope(), attacker_private))

        rows = await wait_for(
            "SELECT event_type FROM audit_log WHERE event_type = 'agent.mode_changed'"
        )
        assert rows
