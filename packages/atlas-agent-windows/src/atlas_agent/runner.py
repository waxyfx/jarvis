"""The agent's own gate in front of every tool.

The backend already ran the Policy Engine before sending anything here. This
runs the checks again anyway, because the two sides are not equivalent:

* the server matches path strings; only this machine can resolve what a path
  really points at;
* the server knows the mode the agent last *reported*; the agent knows the mode
  it is actually in;
* the server can be wrong, or compromised.

So the agent re-derives risk from the same manifests and refuses on any
disagreement. A command that arrives claiming to be LOW when this machine
computes HIGH is not executed at a lower bar — it is not executed at all.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from atlas_agent.logging import get_logger
from atlas_agent.safety.mode import SafeModeController
from atlas_agent.safety.paths import PathGuard, PathRefusedError
from atlas_agent.tools import ExecutionContext, ToolExecutionError, executor_for
from atlas_shared.enums import RefusalReason, RiskLevel, ToolStatus
from atlas_shared.protocol.messages import ToolExecute, ToolFailure, ToolResult
from atlas_shared.tools.catalog import CATALOG
from atlas_shared.tools.manifest import ManifestEvaluationError, RiskContext

__all__ = ["ToolRunner"]

log = get_logger(__name__)


class ToolRunner:
    def __init__(
        self,
        *,
        safe_mode: SafeModeController,
        path_guard: PathGuard,
        risk_context: RiskContext,
        on_activity: Callable[[bool], None] | None = None,
    ) -> None:
        self._safe_mode = safe_mode
        self._context = ExecutionContext(path_guard=path_guard, risk_context=risk_context)
        #: Told when a command starts and stops. The voice engine uses it to
        #: show Executing rather than Thinking; nothing here depends on it, and
        #: a listener that raises must not take the command down with it.
        self._on_activity = on_activity

    async def run(self, command: ToolExecute) -> ToolResult:
        """Execute one command, always returning a result rather than raising."""
        started = time.monotonic()
        self._notify(True)
        try:
            return await self._run(command, started)
        finally:
            self._notify(False)

    def _notify(self, running: bool) -> None:
        if self._on_activity is None:
            return
        try:
            self._on_activity(running)
        except Exception:  # pragma: no cover - a display cannot break execution
            log.warning("tool_activity_listener_failed", running=running)

    async def _run(self, command: ToolExecute, started: float) -> ToolResult:

        def finish(
            status: ToolStatus,
            *,
            result: dict[str, Any] | None = None,
            failure: ToolFailure | None = None,
            refusal: RefusalReason | None = None,
            risk_local: RiskLevel | None = None,
        ) -> ToolResult:
            return ToolResult(
                call_id=command.call_id,
                tool=command.tool,
                status=status,
                result=result,
                failure=failure,
                refusal=refusal,
                risk_local=risk_local,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        # 1. Is this a tool at all?
        if not CATALOG.has(command.tool):
            return finish(ToolStatus.REFUSED, refusal=RefusalReason.UNKNOWN_TOOL)
        manifest = CATALOG.get(command.tool)

        if manifest.version != command.tool_version:
            return finish(
                ToolStatus.REFUSED,
                refusal=RefusalReason.ARGS_INVALID,
                failure=ToolFailure(
                    code="version_mismatch",
                    message=(
                        f"server sent {command.tool} v{command.tool_version}; "
                        f"this agent has v{manifest.version}"
                    ),
                ),
            )

        # 2. Do the arguments match the declared schema?
        try:
            parsed_args = manifest.validate_args(command.args)
        except ValidationError as exc:
            return finish(
                ToolStatus.REFUSED,
                refusal=RefusalReason.ARGS_INVALID,
                failure=ToolFailure(
                    code="args_invalid", message=str(exc.error_count()) + " invalid argument(s)"
                ),
            )

        # 3. What does *this* machine think the risk is?
        try:
            assessment = manifest.assess(command.args, self._context.risk_context)
        except ManifestEvaluationError as exc:
            return finish(
                ToolStatus.REFUSED,
                refusal=RefusalReason.RISK_TOO_HIGH_LOCALLY,
                failure=ToolFailure(code="risk_unevaluable", message=str(exc)),
            )
        risk_local = assessment.level

        if risk_local is RiskLevel.DENY:
            return finish(
                ToolStatus.REFUSED,
                refusal=RefusalReason.RISK_TOO_HIGH_LOCALLY,
                risk_local=risk_local,
            )

        # 4. Disagreement with the server is itself a reason to stop.
        if risk_local.rank > command.risk.rank:
            log.warning(
                "agent_risk_disagreement",
                tool=command.tool,
                server_risk=command.risk.value,
                local_risk=risk_local.value,
            )
            return finish(
                ToolStatus.REFUSED,
                refusal=RefusalReason.RISK_TOO_HIGH_LOCALLY,
                risk_local=risk_local,
                failure=ToolFailure(
                    code="risk_disagreement",
                    message=(
                        f"this machine assesses {risk_local.value}, the server sent "
                        f"{command.risk.value}"
                    ),
                ),
            )

        # 5. SAFE MODE. Checked after risk so the refusal names the real reason,
        #    and checked here — not only on the server — because this is the
        #    only copy of the mode that cannot be changed remotely.
        if self._safe_mode.is_safe and risk_local.rank > RiskLevel.LOW.rank:
            return finish(
                ToolStatus.REFUSED, refusal=RefusalReason.SAFE_MODE, risk_local=risk_local
            )

        # 6. Is anything actually bound to this manifest?
        executor = executor_for(command.tool)
        if executor is None:
            return finish(ToolStatus.NOT_IMPLEMENTED, risk_local=risk_local)

        # 7. Run it, off the event loop, under the deadline.
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(executor, parsed_args, self._context),
                timeout=command.deadline_s,
            )
        except TimeoutError:
            return finish(ToolStatus.TIMEOUT, risk_local=risk_local)
        except PathRefusedError as exc:
            # The guard had the last word, as designed.
            return finish(ToolStatus.REFUSED, refusal=exc.reason, risk_local=risk_local)
        except ToolExecutionError as exc:
            return finish(
                ToolStatus.ERROR,
                failure=ToolFailure(code=exc.code, message=exc.message),
                risk_local=risk_local,
            )
        except Exception as exc:
            log.exception("tool_crashed", tool=command.tool)
            return finish(
                ToolStatus.ERROR,
                failure=ToolFailure(code="unhandled", message=type(exc).__name__),
                risk_local=risk_local,
            )

        return finish(ToolStatus.OK, result=result, risk_local=risk_local)
