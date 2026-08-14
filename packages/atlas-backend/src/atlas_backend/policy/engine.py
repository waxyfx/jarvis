"""The Policy Engine: what ATLAS is allowed to do, and when.

This is the layer that stands between "something asked for an action" and "the
action happens". It is a **pure function** — no I/O, no clock of its own, no
model. Everything it needs arrives in :class:`PolicyRequest`, so every decision
is reproducible and every rule is testable in isolation.

From M3 a language model will propose tool calls. It will propose them *into*
this function, which does not consult it, cannot be persuaded by it, and does
not read its reasoning. That separation is the reason an AI assistant with
access to a computer can be safe: the model suggests, deterministic code
decides.

Rules are applied in a fixed order and the outcome can only ever get stricter as
they are evaluated. No rule can loosen a decision another rule already tightened.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from atlas_shared.enums import AgentMode, Decision, RiskLevel, TrustLevel
from atlas_shared.tools.manifest import (
    ManifestEvaluationError,
    RiskContext,
    ToolManifest,
)

__all__ = [
    "OverrideMode",
    "PermissionOverride",
    "PolicyDecision",
    "PolicyRequest",
    "decide",
]


class OverrideMode(StrEnum):
    """A standing user decision about a tool."""

    ALWAYS_ALLOW = "always_allow"
    ALWAYS_CONFIRM = "always_confirm"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class PermissionOverride:
    tool_pattern: str
    mode: OverrideMode
    expires_at: datetime | None = None

    def matches(self, tool_name: str) -> bool:
        """Exact name, or a dotted prefix written as ``fs.*``."""
        if self.tool_pattern.endswith(".*"):
            return tool_name.startswith(self.tool_pattern[:-1])
        return self.tool_pattern == tool_name

    def is_active(self, now: datetime) -> bool:
        return self.expires_at is None or self.expires_at > now


@dataclass(frozen=True, slots=True)
class PolicyRequest:
    tool: ToolManifest
    args: Mapping[str, Any]
    risk_context: RiskContext
    device_trust: TrustLevel
    #: The agent's last reported mode. The agent enforces SAFE MODE itself and
    #: is authoritative; this lets the server refuse early instead of sending a
    #: command that is certain to bounce.
    agent_mode: AgentMode
    now: datetime
    overrides: tuple[PermissionOverride, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision: Decision
    risk: RiskLevel
    reasons: tuple[str, ...]

    @property
    def is_allowed(self) -> bool:
        return self.decision is Decision.ALLOW


def decide(request: PolicyRequest) -> PolicyDecision:
    """Return the verdict for one proposed tool invocation."""
    reasons: list[str] = []

    # 1. Risk from the manifest. An unevaluable rule is a broken guard, and the
    #    only safe reading of a broken guard is "stop".
    try:
        assessment = request.tool.assess(request.args, request.risk_context)
    except ManifestEvaluationError as exc:
        return PolicyDecision(
            decision=Decision.DENY,
            risk=RiskLevel.DENY,
            reasons=(f"risk could not be evaluated: {exc}",),
        )

    risk = assessment.level
    reasons.extend(assessment.applied_rules)

    # 2. A tool whose rules put it at DENY is structurally forbidden. There is no
    #    confirmation path out of this — that is what distinguishes DENY from HIGH.
    if risk is RiskLevel.DENY:
        return PolicyDecision(Decision.DENY, risk, (*reasons, "action is forbidden by policy"))

    # 3. Device trust. A limited device may read, never act.
    if request.device_trust is not TrustLevel.TRUSTED and risk.rank > RiskLevel.LOW.rank:
        return PolicyDecision(
            Decision.DENY,
            risk,
            (*reasons, f"device trust '{request.device_trust}' permits only low-risk tools"),
        )

    active_overrides = tuple(
        override
        for override in request.overrides
        if override.is_active(request.now) and override.matches(request.tool.name)
    )

    # 4. An explicit user denial outranks everything below it.
    if any(override.mode is OverrideMode.DENY for override in active_overrides):
        return PolicyDecision(Decision.DENY, risk, (*reasons, "denied by a user permission rule"))

    # 5. SAFE MODE. The agent refuses anyway; refusing here keeps the audit trail
    #    honest about why nothing happened.
    if request.agent_mode is AgentMode.SAFE and risk.rank > RiskLevel.LOW.rank:
        return PolicyDecision(
            Decision.DENY, risk, (*reasons, "agent is in SAFE MODE; only low-risk local reads run")
        )

    # 6. Baseline mapping from risk to decision.
    if risk is RiskLevel.HIGH:
        # Deliberately not relaxable by an override: a standing "always allow"
        # must never pre-authorise mass deletion or an unknown executable.
        decision = Decision.CONFIRM
        reasons.append("high risk always requires explicit confirmation")
    elif risk is RiskLevel.MEDIUM:
        if any(override.mode is OverrideMode.ALWAYS_ALLOW for override in active_overrides):
            decision = Decision.ALLOW
            reasons.append("pre-authorised by a user permission rule")
        else:
            decision = Decision.CONFIRM
            reasons.append("medium risk requires confirmation")
    else:
        decision = Decision.ALLOW

    # 7. A user may always ask for *more* friction than the default.
    if decision is Decision.ALLOW and any(
        override.mode is OverrideMode.ALWAYS_CONFIRM for override in active_overrides
    ):
        decision = Decision.CONFIRM
        reasons.append("confirmation requested by a user permission rule")

    return PolicyDecision(decision, risk, tuple(reasons))
