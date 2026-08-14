"""M3 acceptance: natural language all the way to a running program.

    user text → model → tool call → validation → Policy Engine → confirmation
              → signed command → agent → execution → signed result → reply

The model is scripted here so the assertions are about *the pipeline*, not about
whether a particular model phrased something a particular way. Whether Gemini
picks the right tool for a Russian sentence is a separate question, measured in
``test_gemini_live.py`` against the real API.

Everything below the model is production code: real policy, real signatures, a
real agent process, a real database.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from atlas_agent.safety.mode import ModeChangeSource
from atlas_backend.ai import ScriptedProvider, text_reply, tool_reply
from e2e.conftest import E2E_BOOTSTRAP_TOKEN, backend_settings, query, requires_e2e_db
from e2e.harness import AssistantSession, start_stack

pytestmark = [requires_e2e_db, pytest.mark.integration]


Session = AssistantSession


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "report.pdf").write_text("pretend pdf", encoding="utf-8")
    (allowed / "notes.txt").write_text("hello", encoding="utf-8")
    return tmp_path


@pytest.fixture
def allowed_file_roots(workspace: Path) -> tuple[str, ...]:
    return (str(workspace / "allowed"),)


@pytest.fixture
async def session_factory(tmp_path: Path, workspace: Path, allowed_file_roots: tuple[str, ...]):  # type: ignore[no-untyped-def]
    """Start a stack whose model answers from a script.

    The same harness the live acceptance tests use; only the provider differs.
    """
    running: list[Any] = []

    async def start(script: list[Any]) -> Session:
        stack = await start_stack(
            provider=ScriptedProvider(script),
            settings_factory=backend_settings,
            tmp_path=tmp_path,
            workspace=workspace,
            allowed_roots=allowed_file_roots,
            bootstrap_token=E2E_BOOTSTRAP_TOKEN,
            device_name="m3-agent",
        )
        running.append(stack)
        return stack.session

    try:
        yield start
    finally:
        for stack in running:
            await stack.shutdown()


# ------------------------------------------------------- demo scenarios


class TestDemonstration:
    async def test_open_notepad(self, session_factory) -> None:  # type: ignore[no-untyped-def]
        """«Открой Notepad» → app.launch → the agent actually starts it."""
        session = await session_factory(
            [
                tool_reply(("app.launch", {"name": "notepad"})),
                text_reply("Открыл Notepad."),
            ]
        )
        answer = await session.say("Открой Notepad")

        assert answer["stopped_because"] == "completed"
        assert len(answer["executed"]) == 1
        call = answer["executed"][0]
        assert call["tool"] == "app.launch"
        assert call["decision"] == "allow"
        assert call["status"] == "completed"
        assert call["result"]["pid"] > 0

        # Clean up the process this test really started.
        import psutil

        for process in psutil.process_iter(["pid"]):
            if process.info["pid"] == call["result"]["pid"]:
                process.kill()

    async def test_show_memory_usage(self, session_factory) -> None:  # type: ignore[no-untyped-def]
        """«Покажи использование RAM» → system.metrics → natural-language answer."""
        session = await session_factory(
            [
                tool_reply(("system.metrics", {})),
                text_reply("Занято 61% оперативной памяти из 15 ГБ."),
            ]
        )
        answer = await session.say("Покажи использование RAM")

        assert answer["reply"] == "Занято 61% оперативной памяти из 15 ГБ."
        result = answer["executed"][0]["result"]
        assert result["ram_total_mb"] > 0
        assert 0 <= result["ram_used_pct"] <= 100

        # The model was shown the real numbers before it answered.
        second_request = session.provider.requests[1]
        assert any("ram_total_mb" in segment.text for segment in second_request.segments)

    async def test_close_notepad_requires_confirmation(self, session_factory) -> None:  # type: ignore[no-untyped-def]
        """«Закрой Notepad» → app.close → held → confirmed → agent."""
        session = await session_factory(
            [
                tool_reply(("app.close", {"name": "notepad", "force": False})),
                text_reply("Нужно ваше подтверждение, чтобы закрыть Notepad."),
            ]
        )
        answer = await session.say("Закрой Notepad")

        assert answer["executed"] == []
        held = answer["pending_confirmation"][0]
        assert held["tool"] == "app.close"
        assert held["status"] == "pending_confirmation"

        confirmed = await session.confirm(held["id"])
        assert confirmed["status"] == "completed"

        events = [row[0] for row in await query("SELECT event_type FROM audit_log ORDER BY seq")]
        assert "tool.confirmation_required" in events
        assert "tool.confirmed" in events

    async def test_multi_tool_request(self, session_factory) -> None:  # type: ignore[no-untyped-def]
        """«Открой Notepad и покажи использование памяти» → two controlled calls."""
        session = await session_factory(
            [
                tool_reply(
                    ("app.launch", {"name": "notepad"}),
                    ("system.metrics", {}),
                ),
                text_reply("Notepad открыт, память занята на 61%."),
            ]
        )
        answer = await session.say("Открой Notepad и покажи использование памяти")

        assert [call["tool"] for call in answer["executed"]] == [
            "app.launch",
            "system.metrics",
        ]
        assert all(call["status"] == "completed" for call in answer["executed"])
        assert answer["iterations"] == 2  # one to propose, one to summarise

        import psutil

        pid = answer["executed"][0]["result"]["pid"]
        for process in psutil.process_iter(["pid"]):
            if process.info["pid"] == pid:
                process.kill()


# ------------------------------------------------------------ the guards


class TestGuardsHoldEndToEnd:
    async def test_safe_mode_stops_a_model_driven_action(self, session_factory) -> None:  # type: ignore[no-untyped-def]
        session = await session_factory(
            [
                tool_reply(("app.close", {"name": "notepad", "force": False})),
                text_reply("Не удалось."),
            ]
        )
        session.controller.enter_safe_mode("kill switch", ModeChangeSource.LOCAL_HOTKEY)
        await asyncio.sleep(0.4)

        answer = await session.say("Закрой Notepad")

        # Either the server declined to dispatch, or the agent refused. What must
        # not happen is execution.
        assert answer["executed"] == []

    async def test_a_search_outside_the_roots_is_denied(self, session_factory) -> None:  # type: ignore[no-untyped-def]
        session = await session_factory(
            [
                tool_reply(("fs.search", {"query": "*", "root": "C:/Windows"})),
                text_reply("Отказано."),
            ]
        )
        answer = await session.say("Найди всё в системной папке")

        assert answer["denied"]
        assert answer["executed"] == []

    async def test_an_allowed_search_finds_the_file(self, session_factory, workspace: Path) -> None:  # type: ignore[no-untyped-def]
        session = await session_factory(
            [
                tool_reply(
                    (
                        "fs.search",
                        {"query": "report.pdf", "root": str(workspace / "allowed")},
                    )
                ),
                text_reply("Нашёл report.pdf."),
            ]
        )
        answer = await session.say("Найди файл report.pdf")

        result = answer["executed"][0]["result"]
        assert result["count"] == 1
        assert "report.pdf" in result["matches"][0]["path"]

    async def test_an_invented_tool_never_reaches_the_agent(self, session_factory) -> None:  # type: ignore[no-untyped-def]
        session = await session_factory(
            [
                tool_reply(("powershell.run", {"command": "Get-Process"})),
                text_reply("Такой возможности нет."),
            ]
        )
        answer = await session.say("Выполни Get-Process в PowerShell")

        assert answer["rejected"][0]["reason"] == "unknown_tool"
        assert await query("SELECT count(*) FROM tool_calls") == [(0,)]
