"""The registry of tools ATLAS may be asked to run.

Declaring a tool here does *not* make it runnable. A manifest is a contract:
the Policy Engine (M3) uses it to assess risk and the agent (M2) binds an
executor to it. Until an executor exists, an execution request for a declared
tool fails with :attr:`~atlas_shared.protocol.errors.ErrorCode.TOOL_NOT_IMPLEMENTED`.
M1 ships the declarations and the risk machinery; nothing here executes.

Risk classes follow docs/PHASE-0-ARCHITECTURE.md §10.3.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from atlas_shared.enums import RiskLevel
from atlas_shared.tools.manifest import (
    Condition,
    ConditionOp,
    RiskRule,
    ToolDescriptor,
    ToolManifest,
)

__all__ = ["CATALOG", "ToolCatalog"]


class ToolCatalog:
    """An ordered, immutable-after-registration set of tool manifests."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolManifest] = {}

    def register(self, manifest: ToolManifest) -> ToolManifest:
        if manifest.name in self._tools:
            raise RuntimeError(f"duplicate tool registration: {manifest.name}")
        self._tools[manifest.name] = manifest
        return manifest

    def get(self, name: str) -> ToolManifest:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"unknown tool: {name}") from None

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def all(self) -> tuple[ToolManifest, ...]:
        return tuple(self._tools[name] for name in sorted(self._tools))

    def descriptors(self) -> tuple[ToolDescriptor, ...]:
        return tuple(manifest.to_descriptor() for manifest in self.all())


CATALOG = ToolCatalog()


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------
# system
# --------------------------------------------------------------------------


class SystemMetricsArgs(_Args):
    """No arguments: returns CPU, RAM, disk, network and uptime."""


CATALOG.register(
    ToolManifest(
        name="system.metrics",
        version=1,
        summary="Read CPU, memory, disk, network and uptime counters.",
        args_model=SystemMetricsArgs,
        base_risk=RiskLevel.LOW,
        reversible=True,
        timeout_s=5.0,
        requires_capabilities=("system",),
        rate_limit_per_minute=60,
    )
)


# --------------------------------------------------------------------------
# applications
# --------------------------------------------------------------------------


class AppListArgs(_Args):
    include_store_apps: bool = Field(
        default=False,
        description="Also list installed applications, not only running processes.",
    )


CATALOG.register(
    ToolManifest(
        name="app.list",
        version=1,
        summary="List running processes and installed applications.",
        args_model=AppListArgs,
        base_risk=RiskLevel.LOW,
        reversible=True,
        timeout_s=15.0,
        requires_capabilities=("apps",),
        rate_limit_per_minute=20,
    )
)


class AppLaunchArgs(_Args):
    name: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "The application as a person would name it: 'chrome', 'vs code', "
            "'notepad'. Do not pass a file path here."
        ),
    )
    arguments: tuple[str, ...] = Field(
        default=(), description="Command-line arguments, if the user asked for any."
    )
    #: Set only when the caller resolved a concrete binary. An explicit path
    #: outside the known install roots is treated as an unknown executable.
    executable_path: str | None = Field(
        default=None,
        description=(
            "Full path to an executable. Only set this if the user gave an exact "
            "path; otherwise leave it out and use 'name'."
        ),
    )


CATALOG.register(
    ToolManifest(
        name="app.launch",
        version=1,
        summary="Start an application by friendly name or resolved path.",
        args_model=AppLaunchArgs,
        base_risk=RiskLevel.LOW,
        reversible=True,
        timeout_s=30.0,
        requires_capabilities=("apps",),
        side_effects=("process",),
        rate_limit_per_minute=20,
        escalations=(
            RiskRule(
                to=RiskLevel.HIGH,
                reason="executable outside known install roots (unknown binary)",
                conditions=(
                    Condition(
                        field="executable_path",
                        op=ConditionOp.PATH_OUTSIDE_ROOTS,
                        roots="executables",
                        default=None,
                    ),
                ),
            ),
        ),
    )
)


class AppCloseArgs(_Args):
    name: str | None = Field(
        default=None, description="Application name, e.g. 'notepad'. Either this or pid."
    )
    pid: int | None = Field(default=None, ge=1, description="Process id, when the user gave one.")
    #: Terminate rather than requesting a graceful close — unsaved work is lost.
    force: bool = Field(
        default=False,
        description=(
            "Terminate the process instead of asking it to close. Destroys unsaved "
            "work. Only set this if the user explicitly asked to force it."
        ),
    )


CATALOG.register(
    ToolManifest(
        name="app.close",
        version=1,
        summary="Close an application gracefully, or terminate it when forced.",
        args_model=AppCloseArgs,
        base_risk=RiskLevel.MEDIUM,
        reversible=False,
        timeout_s=20.0,
        requires_capabilities=("apps",),
        side_effects=("process",),
        rate_limit_per_minute=10,
        escalations=(
            RiskRule(
                to=RiskLevel.HIGH,
                reason="forced termination can destroy unsaved work",
                conditions=(Condition(field="force", op=ConditionOp.IS_TRUE, default=False),),
            ),
        ),
    )
)


# --------------------------------------------------------------------------
# filesystem
# --------------------------------------------------------------------------


class FsSearchArgs(_Args):
    query: str = Field(
        min_length=1,
        max_length=300,
        description=(
            "Filename or fragment to look for. Wildcards * and ? are allowed; "
            "without them the query is matched anywhere in the name."
        ),
    )
    root: str = Field(
        min_length=1,
        description=(
            "Absolute directory to search under. It must be one of the user's "
            "allowed folders; if the user did not say where to look, ask."
        ),
    )
    max_results: int = Field(default=50, ge=1, le=1000, description="Cap on results.")


CATALOG.register(
    ToolManifest(
        name="fs.search",
        version=1,
        summary="Find files by name under an allowed root.",
        args_model=FsSearchArgs,
        base_risk=RiskLevel.LOW,
        reversible=True,
        timeout_s=30.0,
        requires_capabilities=("fs",),
        rate_limit_per_minute=30,
        escalations=(
            RiskRule(
                to=RiskLevel.DENY,
                reason="search root is outside the allowed file roots",
                conditions=(Condition(field="root", op=ConditionOp.PATH_OUTSIDE_ROOTS),),
            ),
        ),
    )
)


class FsOpenArgs(_Args):
    path: str = Field(
        min_length=1,
        description="Absolute path to the file, inside one of the allowed folders.",
    )


CATALOG.register(
    ToolManifest(
        name="fs.open",
        version=1,
        summary="Open a file with its default application.",
        args_model=FsOpenArgs,
        base_risk=RiskLevel.LOW,
        reversible=True,
        timeout_s=20.0,
        requires_capabilities=("fs",),
        side_effects=("process",),
        rate_limit_per_minute=20,
        escalations=(
            RiskRule(
                to=RiskLevel.DENY,
                reason="path is outside the allowed file roots",
                conditions=(Condition(field="path", op=ConditionOp.PATH_OUTSIDE_ROOTS),),
            ),
            RiskRule(
                to=RiskLevel.HIGH,
                reason="opening an executable or script runs code",
                conditions=(
                    Condition(
                        field="path",
                        op=ConditionOp.MATCHES,
                        value=r"\.(exe|com|scr|bat|cmd|ps1|psm1|vbs|js|jar|msi|reg)$",
                    ),
                ),
            ),
        ),
    )
)


class FsDeleteArgs(_Args):
    paths: tuple[str, ...] = Field(min_length=1)
    recursive: bool = False


CATALOG.register(
    ToolManifest(
        name="fs.delete",
        version=1,
        # Reversible because the executor moves items to the Recycle Bin. ATLAS
        # has no unrecoverable delete, by design (PHASE-0 §10.3).
        summary="Move files or folders to the Recycle Bin.",
        args_model=FsDeleteArgs,
        base_risk=RiskLevel.MEDIUM,
        reversible=True,
        timeout_s=60.0,
        requires_capabilities=("fs",),
        side_effects=("filesystem",),
        rate_limit_per_minute=10,
        escalations=(
            RiskRule(
                to=RiskLevel.DENY,
                reason="at least one path is outside the allowed file roots",
                conditions=(Condition(field="paths", op=ConditionOp.PATH_OUTSIDE_ROOTS),),
            ),
            RiskRule(
                to=RiskLevel.HIGH,
                reason="recursive delete affects an unbounded number of files",
                conditions=(Condition(field="recursive", op=ConditionOp.IS_TRUE, default=False),),
            ),
            RiskRule(
                to=RiskLevel.HIGH,
                # NOTE: this counts the *requested* paths. The true number of
                # affected files is knowable only on the agent, which re-assesses
                # after a dry run before touching anything.
                reason="bulk delete of more than 20 targets",
                conditions=(Condition(field="paths", op=ConditionOp.LENGTH_GT, value=20),),
            ),
        ),
    )
)
