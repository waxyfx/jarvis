"""A running ATLAS: backend, agent and a provider of your choosing.

Shared by the scripted end-to-end tests and the live Gemini acceptance tests, so
both exercise exactly the same stack. The only difference between them is which
``AIProvider`` is handed in.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from atlas_agent.backend import BackendClient
from atlas_agent.config import AgentSettings
from atlas_agent.identity import IdentityStore
from atlas_agent.runner import ToolRunner
from atlas_agent.safety.mode import SafeModeController
from atlas_agent.safety.paths import PathGuard
from atlas_agent.transport import AgentTransport
from atlas_shared.tools.manifest import RiskContext

__all__ = ["AssistantSession", "RunningStack", "start_stack"]


@dataclass
class AssistantSession:
    """A paired, connected ATLAS you can talk to."""

    url: str
    token: str
    provider: Any
    controller: SafeModeController
    workspace: Path

    async def say(self, text: str, language: str = "ru") -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.url, timeout=120.0) as client:
            response = await client.post(
                "/v1/assistant/message",
                json={"text": text, "language": language},
                headers={"Authorization": f"Bearer {self.token}"},
            )
        assert response.status_code == 200, response.text
        return dict(response.json())

    async def confirm(self, call_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.url, timeout=120.0) as client:
            response = await client.post(
                f"/v1/tools/calls/{call_id}/confirm",
                headers={"Authorization": f"Bearer {self.token}"},
            )
        assert response.status_code == 200, response.text
        return dict(response.json())


@dataclass
class RunningStack:
    session: AssistantSession
    stop: asyncio.Event
    task: asyncio.Task[None]
    server: uvicorn.Server
    thread: threading.Thread

    async def shutdown(self) -> None:
        self.stop.set()
        await asyncio.wait_for(self.task, timeout=20)
        self.server.should_exit = True
        for _ in range(200):
            if not self.thread.is_alive():
                break
            await asyncio.sleep(0.05)


async def start_stack(
    *,
    provider: Any,
    settings_factory: Any,
    tmp_path: Path,
    workspace: Path,
    allowed_roots: tuple[str, ...],
    bootstrap_token: str,
    device_name: str = "e2e-agent",
) -> RunningStack:
    """Start a backend on a free port and connect a real agent to it."""
    from atlas_backend.main import create_app

    app = create_app(settings_factory(allowed_roots), ai_provider=provider)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="e2e-backend")
    thread.start()

    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    if not server.started:
        server.should_exit = True
        raise RuntimeError("the backend did not start")

    url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"

    agent_settings = AgentSettings(
        backend_url=url,
        identity_path=tmp_path / "identity.json",
        mode_state_path=tmp_path / "mode.json",
        allow_plaintext_key=True,
        device_name=device_name,
        request_timeout_s=15.0,
        reconnect_initial_s=0.2,
        monitor_enabled=False,
    )
    store = IdentityStore(agent_settings.identity_path, allow_plaintext=True)

    async with httpx.AsyncClient(base_url=url, timeout=15.0) as client:
        started = await client.post(
            "/v1/pair/start",
            json={"kind": "windows_agent", "name": device_name},
            headers={"X-Atlas-Bootstrap-Token": bootstrap_token},
        )
    assert started.status_code == 201, started.text

    identity = await BackendClient(agent_settings).enrol(store.create(), started.json()["code"])
    store.save(identity)

    allowed = workspace / "allowed"
    controller = SafeModeController(agent_settings.mode_state_path)
    transport = AgentTransport(
        agent_settings,
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

    token = await BackendClient(agent_settings).authenticate(identity)
    return RunningStack(
        session=AssistantSession(
            url=url,
            token=token,
            provider=provider,
            controller=controller,
            workspace=workspace,
        ),
        stop=stop,
        task=task,
        server=server,
        thread=thread,
    )
