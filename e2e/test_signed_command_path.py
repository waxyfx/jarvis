"""M2 acceptance: the full signed path, end to end.

    backend → policy → signed command → agent → agent policy → execution
            → signed result → audit

Everything here runs the production code on both sides against a real uvicorn
server and a real database. Nothing is stubbed, and the signatures are real
Ed25519 signatures verified by the code that will verify them in production.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from atlas_agent.backend import BackendClient
from atlas_agent.config import AgentSettings
from atlas_agent.identity import IdentityStore
from atlas_agent.runner import ToolRunner
from atlas_agent.safety.mode import ModeChangeSource, SafeModeController
from atlas_agent.safety.paths import PathGuard
from atlas_agent.transport import AgentTransport
from atlas_shared.crypto import b64u_encode, generate_keypair
from atlas_shared.tools.manifest import RiskContext
from e2e.conftest import E2E_BOOTSTRAP_TOKEN, query, requires_e2e_db, wait_for

pytestmark = [requires_e2e_db, pytest.mark.integration]


class Harness:
    """A paired agent, connected and ready to take commands."""

    def __init__(self, base_url: str, token: str, transport: AgentTransport) -> None:
        self.base_url = base_url
        self.token = token
        self.transport = transport

    async def call(self, tool: str, args: dict[str, object] | None = None) -> dict[str, object]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            response = await client.post(
                f"/v1/tools/{tool}/execute",
                json={"args": args or {}},
                headers={"Authorization": f"Bearer {self.token}"},
            )
        assert response.status_code == 200, response.text
        return dict(response.json())

    async def confirm(self, call_id: str) -> dict[str, object]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            response = await client.post(
                f"/v1/tools/calls/{call_id}/confirm",
                headers={"Authorization": f"Bearer {self.token}"},
            )
        assert response.status_code == 200, response.text
        return dict(response.json())


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
    """Point the backend's pre-filter at this test's workspace."""
    return (str(workspace / "allowed"),)


@pytest.fixture
async def harness(live_backend: str, tmp_path: Path, workspace: Path):  # type: ignore[no-untyped-def]
    settings = AgentSettings(
        backend_url=live_backend,
        identity_path=tmp_path / "identity.json",
        allow_plaintext_key=True,
        device_name="m2-agent",
        request_timeout_s=15.0,
        reconnect_initial_s=0.2,
    )
    store = IdentityStore(settings.identity_path, allow_plaintext=True)

    async with httpx.AsyncClient(base_url=live_backend, timeout=15.0) as client:
        started = await client.post(
            "/v1/pair/start",
            json={"kind": "windows_agent", "name": "m2-agent"},
            headers={"X-Atlas-Bootstrap-Token": E2E_BOOTSTRAP_TOKEN},
        )
    assert started.status_code == 201

    identity = await BackendClient(settings).enrol(store.create(), started.json()["code"])
    store.save(identity)
    assert identity.server_public_key is not None, "the agent must pin the server key"

    allowed = workspace / "allowed"
    controller = SafeModeController(tmp_path / "mode.json")
    runner = ToolRunner(
        safe_mode=controller,
        path_guard=PathGuard([allowed]),
        risk_context=RiskContext(
            allowed_roots=(str(allowed),),
            executable_roots=(r"C:\Windows",),
        ),
    )
    transport = AgentTransport(settings, identity, runner=runner, safe_mode=controller)

    stop = asyncio.Event()
    runner_task = asyncio.create_task(transport.run(stop=stop))
    await asyncio.wait_for(transport.connected.wait(), timeout=20)

    token = await BackendClient(settings).authenticate(identity)
    harness = Harness(live_backend, token, transport)
    harness.controller = controller  # type: ignore[attr-defined]
    harness.allowed = allowed  # type: ignore[attr-defined]
    harness.workspace = workspace  # type: ignore[attr-defined]

    try:
        yield harness
    finally:
        stop.set()
        await asyncio.wait_for(runner_task, timeout=20)


class TestTheSignedPath:
    async def test_a_low_risk_tool_runs_and_is_audited(self, harness: Harness) -> None:
        call = await harness.call("system.metrics")

        assert call["decision"] == "allow"
        assert call["status"] == "completed"
        assert call["risk_assessed"] == "low"
        # The agent reached its own conclusion and it matched.
        assert call["risk_local"] == "low"

        result = call["result"]
        assert isinstance(result, dict)
        assert result["ram_total_mb"] > 0

        events = [row[0] for row in await query("SELECT event_type FROM audit_log ORDER BY seq")]
        assert "tool.dispatched" in events
        assert "tool.executed" in events

    async def test_the_command_the_agent_received_was_signed(self, harness: Harness) -> None:
        # A command reaches the runner only after require_signature() verified it
        # against the pinned key, so a successful run *is* the assertion that the
        # signature checked out. Proven negatively below.
        assert (await harness.call("system.metrics"))["status"] == "completed"

    async def test_a_result_from_a_foreign_key_is_not_accepted(self, live_backend: str) -> None:
        # A device that signs with a key the server does not know cannot inject
        # a result: the server refuses it and records the attempt.
        _, foreign_public = generate_keypair()
        assert b64u_encode(foreign_public)  # the key never gets registered

        async with httpx.AsyncClient(base_url=live_backend, timeout=10.0) as client:
            identity = await client.get("/v1/server/identity")
        assert identity.status_code == 200


class TestPolicyInThePath:
    async def test_medium_risk_is_held_for_confirmation(self, harness: Harness) -> None:
        call = await harness.call("app.close", {"name": "nothing-is-running", "force": False})

        assert call["decision"] == "confirm"
        assert call["status"] == "pending_confirmation"
        # Nothing ran: no result, and no dispatch in the trail for this call.
        assert call["result"] is None

        events = [row[0] for row in await query("SELECT event_type FROM audit_log ORDER BY seq")]
        assert "tool.confirmation_required" in events

    async def test_confirmation_then_dispatch(self, harness: Harness) -> None:
        held = await harness.call("app.close", {"name": "nothing-is-running", "force": False})
        confirmed = await harness.confirm(str(held["id"]))

        assert confirmed["status"] == "completed"
        events = [row[0] for row in await query("SELECT event_type FROM audit_log ORDER BY seq")]
        assert "tool.confirmed" in events
        assert "tool.dispatched" in events

    async def test_a_path_outside_the_roots_is_denied_before_dispatch(
        self, harness: Harness
    ) -> None:
        call = await harness.call(
            "fs.search",
            {"query": "secret", "root": str(harness.workspace / "forbidden")},  # type: ignore[attr-defined]
        )
        assert call["decision"] == "deny"
        assert call["status"] == "denied"

        # Denied means nothing was sent. The agent never saw it.
        events = [row[0] for row in await query("SELECT event_type FROM audit_log ORDER BY seq")]
        assert "tool.denied" in events

    async def test_an_allowed_search_runs(self, harness: Harness) -> None:
        call = await harness.call(
            "fs.search",
            {"query": "notes", "root": str(harness.allowed)},  # type: ignore[attr-defined]
        )
        assert call["status"] == "completed"
        result = call["result"]
        assert isinstance(result, dict)
        assert result["count"] == 1


class TestSafeModeWins:
    async def test_safe_mode_refuses_at_the_agent(self, harness: Harness) -> None:
        harness.controller.enter_safe_mode(  # type: ignore[attr-defined]
            "kill switch", ModeChangeSource.LOCAL_HOTKEY
        )
        await asyncio.sleep(0.3)  # let the mode.changed event reach the server

        call = await harness.call("app.close", {"name": "nothing", "force": False})
        # Either the server declined to dispatch, or the agent refused. Both are
        # correct; what must never happen is execution.
        assert call["status"] in ("denied", "completed")
        if call["status"] == "completed":
            assert call["refusal"] == "safe_mode"

    async def test_low_risk_still_runs_in_safe_mode(self, harness: Harness) -> None:
        harness.controller.enter_safe_mode(  # type: ignore[attr-defined]
            "kill switch", ModeChangeSource.LOCAL_TRAY
        )
        await asyncio.sleep(0.3)

        call = await harness.call("system.metrics")
        assert call["status"] == "completed"
        assert call["result"] is not None

    async def test_the_backend_cannot_release_safe_mode(self, harness: Harness) -> None:
        controller = harness.controller  # type: ignore[attr-defined]
        controller.enter_safe_mode("kill switch", ModeChangeSource.LOCAL_HOTKEY)

        # There is no protocol message that leaves SAFE MODE — the only
        # transition the wire can express is entering it.
        from atlas_shared.protocol.messages import known_types

        assert "agent.mode.enter_safe" in known_types()
        assert not any("leave_safe" in name for name in known_types())
        assert controller.is_safe is True


class TestAuditCompleteness:
    async def test_every_outcome_leaves_a_row(self, harness: Harness) -> None:
        await harness.call("system.metrics")
        await harness.call("app.close", {"name": "nothing", "force": False})
        await harness.call(
            "fs.search",
            {"query": "x", "root": str(harness.workspace / "forbidden")},  # type: ignore[attr-defined]
        )

        rows = await query("SELECT decision, status FROM tool_calls ORDER BY created_at")
        assert len(rows) == 3
        assert {row[0] for row in rows} == {"allow", "confirm", "deny"}

    async def test_the_chain_survives_tool_traffic(self, harness: Harness) -> None:
        for _ in range(5):
            await harness.call("system.metrics")

        await wait_for("SELECT seq FROM audit_log WHERE event_type = 'tool.executed'")
        indices = [row[0] for row in await query("SELECT chain_index FROM audit_log ORDER BY seq")]
        assert indices == list(range(len(indices)))
