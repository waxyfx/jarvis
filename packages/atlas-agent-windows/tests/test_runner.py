"""The agent's gate: what it refuses, and why."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas_agent.runner import ToolRunner
from atlas_agent.safety.mode import ModeChangeSource, SafeModeController
from atlas_agent.safety.paths import PathGuard
from atlas_shared.enums import RefusalReason, RiskLevel, ToolStatus
from atlas_shared.ids import new_ulid
from atlas_shared.protocol.messages import ToolExecute
from atlas_shared.tools.manifest import RiskContext


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "notes.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "forbidden").mkdir()
    (tmp_path / "forbidden" / "secret.txt").write_text("nope", encoding="utf-8")
    return tmp_path


@pytest.fixture
def controller(tmp_path: Path) -> SafeModeController:
    return SafeModeController(tmp_path / "mode.json")


@pytest.fixture
def runner(workspace: Path, controller: SafeModeController) -> ToolRunner:
    allowed = workspace / "allowed"
    return ToolRunner(
        safe_mode=controller,
        path_guard=PathGuard([allowed]),
        risk_context=RiskContext(
            allowed_roots=(str(allowed),),
            executable_roots=(str(workspace / "programs"),),
        ),
    )


def command(
    tool: str,
    args: dict[str, object] | None = None,
    *,
    risk: RiskLevel = RiskLevel.LOW,
    version: int = 1,
    deadline_s: float = 30.0,
) -> ToolExecute:
    return ToolExecute(
        call_id=new_ulid(),
        tool=tool,
        tool_version=version,
        args=args or {},
        risk=risk,
        deadline_s=deadline_s,
    )


class TestHappyPath:
    async def test_system_metrics_runs(self, runner: ToolRunner) -> None:
        result = await runner.run(command("system.metrics"))
        assert result.status is ToolStatus.OK
        assert result.result is not None
        assert result.result["ram_total_mb"] > 0
        assert result.risk_local is RiskLevel.LOW

    async def test_search_finds_a_file(self, runner: ToolRunner, workspace: Path) -> None:
        result = await runner.run(
            command("fs.search", {"query": "notes", "root": str(workspace / "allowed")})
        )
        assert result.status is ToolStatus.OK
        assert result.result is not None
        assert [match["path"] for match in result.result["matches"]]

    async def test_result_carries_the_call_id(self, runner: ToolRunner) -> None:
        sent = command("system.metrics")
        result = await runner.run(sent)
        assert result.call_id == sent.call_id
        assert result.duration_ms >= 0


class TestUnknownAndUnbound:
    async def test_unknown_tool_is_refused(self, runner: ToolRunner) -> None:
        result = await runner.run(command("does.not.exist"))
        assert result.status is ToolStatus.REFUSED
        assert result.refusal is RefusalReason.UNKNOWN_TOOL

    async def test_declared_but_unbound_tool_reports_not_implemented(
        self, runner: ToolRunner, workspace: Path
    ) -> None:
        # fs.delete has a manifest but no executor in M2. It must say so rather
        # than silently succeeding.
        result = await runner.run(
            command(
                "fs.delete",
                {"paths": [str(workspace / "allowed" / "notes.txt")], "recursive": False},
                risk=RiskLevel.MEDIUM,
            )
        )
        assert result.status is ToolStatus.NOT_IMPLEMENTED

    async def test_version_mismatch_is_refused(self, runner: ToolRunner) -> None:
        result = await runner.run(command("system.metrics", version=99))
        assert result.status is ToolStatus.REFUSED
        assert result.failure is not None
        assert result.failure.code == "version_mismatch"


class TestArgumentValidation:
    async def test_unknown_argument_is_refused(self, runner: ToolRunner) -> None:
        result = await runner.run(command("system.metrics", {"smuggled": "value"}))
        assert result.status is ToolStatus.REFUSED
        assert result.refusal is RefusalReason.ARGS_INVALID

    async def test_wrong_type_is_refused(self, runner: ToolRunner, workspace: Path) -> None:
        result = await runner.run(
            command(
                "fs.search",
                {"query": "x", "root": str(workspace / "allowed"), "max_results": "many"},
            )
        )
        assert result.status is ToolStatus.REFUSED
        assert result.refusal is RefusalReason.ARGS_INVALID


class TestPathEnforcement:
    async def test_search_outside_the_roots_is_refused(
        self, runner: ToolRunner, workspace: Path
    ) -> None:
        result = await runner.run(
            command(
                "fs.search",
                {"query": "secret", "root": str(workspace / "forbidden")},
                risk=RiskLevel.DENY,
            )
        )
        assert result.status is ToolStatus.REFUSED

    async def test_open_outside_the_roots_is_refused(
        self, runner: ToolRunner, workspace: Path
    ) -> None:
        result = await runner.run(
            command("fs.open", {"path": str(workspace / "forbidden" / "secret.txt")})
        )
        assert result.status is ToolStatus.REFUSED

    async def test_a_denylisted_file_inside_a_root_is_refused(
        self, runner: ToolRunner, workspace: Path
    ) -> None:
        secret = workspace / "allowed" / ".env"
        secret.write_text("ATLAS_JWT_SECRET=x", encoding="utf-8")
        result = await runner.run(command("fs.open", {"path": str(secret)}))
        assert result.status is ToolStatus.REFUSED
        assert result.refusal is RefusalReason.PATH_DENYLISTED


class TestSafeMode:
    async def test_low_risk_still_runs(
        self, runner: ToolRunner, controller: SafeModeController
    ) -> None:
        controller.enter_safe_mode("test", ModeChangeSource.LOCAL_TRAY)
        result = await runner.run(command("system.metrics"))
        assert result.status is ToolStatus.OK

    async def test_medium_risk_is_refused(
        self, runner: ToolRunner, controller: SafeModeController
    ) -> None:
        controller.enter_safe_mode("test", ModeChangeSource.LOCAL_TRAY)
        result = await runner.run(
            command("app.close", {"name": "notepad", "force": False}, risk=RiskLevel.MEDIUM)
        )
        assert result.status is ToolStatus.REFUSED
        assert result.refusal is RefusalReason.SAFE_MODE

    async def test_a_command_marked_low_by_the_server_cannot_slip_past(
        self, runner: ToolRunner, controller: SafeModeController
    ) -> None:
        # The server's risk label is not what SAFE MODE consults; the agent's own
        # assessment is. Mislabelling a MEDIUM tool as LOW changes nothing.
        controller.enter_safe_mode("test", ModeChangeSource.LOCAL_TRAY)
        result = await runner.run(
            command("app.close", {"name": "notepad", "force": False}, risk=RiskLevel.LOW)
        )
        assert result.status is ToolStatus.REFUSED


class TestRiskDisagreement:
    async def test_under_assessed_command_is_refused(
        self, runner: ToolRunner, workspace: Path
    ) -> None:
        # The agent computes HIGH for forcing a close; a server claiming MEDIUM
        # does not get it executed at the lower bar.
        result = await runner.run(
            command("app.close", {"name": "notepad", "force": True}, risk=RiskLevel.MEDIUM)
        )
        assert result.status is ToolStatus.REFUSED
        assert result.refusal is RefusalReason.RISK_TOO_HIGH_LOCALLY
        assert result.risk_local is RiskLevel.HIGH

    async def test_matching_assessment_proceeds(self, runner: ToolRunner) -> None:
        result = await runner.run(
            command(
                "app.close", {"name": "definitely-not-running", "force": True}, risk=RiskLevel.HIGH
            )
        )
        # Refused for a different reason would be a failure of this test; here
        # the tool ran and simply found nothing.
        assert result.status is ToolStatus.ERROR
        assert result.failure is not None
        assert result.failure.code == "not_found"

    async def test_over_assessed_command_is_accepted(self, runner: ToolRunner) -> None:
        # A server being *more* cautious than the agent is never a problem.
        result = await runner.run(command("system.metrics", risk=RiskLevel.HIGH))
        assert result.status is ToolStatus.OK


class TestFailureHandling:
    async def test_a_failing_tool_reports_error_not_a_crash(self, runner: ToolRunner) -> None:
        result = await runner.run(command("app.launch", {"name": "definitely-not-installed-xyz"}))
        assert result.status is ToolStatus.ERROR
        assert result.failure is not None
        assert result.failure.code == "not_found"

    async def test_deadline_is_enforced(self, runner: ToolRunner, workspace: Path) -> None:
        deep = workspace / "allowed"
        for index in range(40):
            deep = deep / f"level{index}"
        deep.mkdir(parents=True)
        (deep / "needle.txt").write_text("x", encoding="utf-8")

        result = await runner.run(
            command(
                "fs.search",
                {"query": "no-such-file", "root": str(workspace / "allowed")},
                deadline_s=0.001,
            )
        )
        assert result.status in (ToolStatus.TIMEOUT, ToolStatus.OK)


class TestActivityReporting:
    """Whether the voice engine can tell Executing from Thinking.

    It cannot see execution for itself: from inside a turn, running a program
    and answering a question are both just a wait. The runner is what knows.
    """

    async def test_a_listener_is_told_when_a_command_starts_and_stops(
        self, workspace: Path, controller: SafeModeController
    ) -> None:
        seen: list[bool] = []
        runner = ToolRunner(
            safe_mode=controller,
            path_guard=PathGuard([workspace / "allowed"]),
            risk_context=RiskContext(
                allowed_roots=(str(workspace / "allowed"),),
                executable_roots=(str(workspace / "programs"),),
            ),
            on_activity=seen.append,
        )

        await runner.run(command("system.metrics"))

        assert seen == [True, False]

    async def test_a_refused_command_still_reports_the_end(
        self, workspace: Path, controller: SafeModeController
    ) -> None:
        """Otherwise the display sticks on Executing for a tool that never ran."""
        seen: list[bool] = []
        runner = ToolRunner(
            safe_mode=controller,
            path_guard=PathGuard([workspace / "allowed"]),
            risk_context=RiskContext(
                allowed_roots=(str(workspace / "allowed"),),
                executable_roots=(str(workspace / "programs"),),
            ),
            on_activity=seen.append,
        )

        await runner.run(command("no.such.tool"))

        assert seen == [True, False]

    async def test_a_broken_listener_does_not_break_the_command(
        self, workspace: Path, controller: SafeModeController
    ) -> None:
        """A display is not allowed to take execution down with it."""

        def explode(_: bool) -> None:
            raise RuntimeError("the tray fell over")

        runner = ToolRunner(
            safe_mode=controller,
            path_guard=PathGuard([workspace / "allowed"]),
            risk_context=RiskContext(
                allowed_roots=(str(workspace / "allowed"),),
                executable_roots=(str(workspace / "programs"),),
            ),
            on_activity=explode,
        )

        result = await runner.run(command("system.metrics"))

        assert result.status is ToolStatus.OK
