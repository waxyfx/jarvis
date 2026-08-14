"""Executor registry.

Every capability is a separate, named function with a typed argument model.
There is deliberately **no generic "run this command" executor**: a shell tool
would collapse the entire risk model into one entry, because the risk of
`powershell -c ...` depends on a string nobody can classify in advance.

Adding a capability therefore means adding a manifest (risk, escalation rules,
argument schema) *and* an executor bound to it. Neither works without the other,
which is what keeps the catalogue and the policy in step.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from atlas_agent.safety.paths import PathGuard
from atlas_shared.tools.manifest import RiskContext

__all__ = ["ExecutionContext", "ToolExecutionError", "executor_for", "register_executor"]


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Everything an executor is allowed to know about its environment."""

    path_guard: PathGuard
    risk_context: RiskContext


class ToolExecutionError(Exception):
    """A tool ran and failed. Distinct from a refusal, which happens earlier."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


#: Executors are synchronous: they call blocking Windows APIs, and the runner
#: puts them on a worker thread. Making them async would only add ceremony.
Executor = Callable[[Any, ExecutionContext], dict[str, Any]]

_EXECUTORS: dict[str, Executor] = {}

ArgsT = TypeVar("ArgsT", bound=BaseModel)


def register_executor(tool_name: str) -> Callable[[Executor], Executor]:
    def decorator(function: Executor) -> Executor:
        if tool_name in _EXECUTORS:
            raise RuntimeError(f"duplicate executor for {tool_name}")
        _EXECUTORS[tool_name] = function
        return function

    return decorator


def executor_for(tool_name: str) -> Executor | None:
    """The bound executor, or ``None`` when the tool is declared but not built."""
    return _EXECUTORS.get(tool_name)


def registered_tools() -> frozenset[str]:
    return frozenset(_EXECUTORS)
