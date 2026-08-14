"""Windows capabilities, one typed tool at a time.

Importing this package registers every executor. The imports below look unused;
they are the registration.
"""

from atlas_agent.tools import apps, files, system  # noqa: F401
from atlas_agent.tools.base import (
    ExecutionContext,
    ToolExecutionError,
    executor_for,
    register_executor,
    registered_tools,
)

__all__ = [
    "ExecutionContext",
    "ToolExecutionError",
    "executor_for",
    "register_executor",
    "registered_tools",
]
