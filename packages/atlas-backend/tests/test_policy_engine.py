"""The Policy Engine decides what may run. These tests pin every rule.

No database, no network: the engine is a pure function, and that is precisely
what makes exhaustive testing of the security-critical path cheap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from atlas_backend.policy import (
    OverrideMode,
    PermissionOverride,
    PolicyRequest,
    decide,
)
from atlas_shared.enums import AgentMode, Decision, RiskLevel, TrustLevel
from atlas_shared.tools.catalog import CATALOG
from atlas_shared.tools.manifest import (
    Condition,
    ConditionOp,
    RiskContext,
    RiskRule,
    ToolManifest,
)

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)

ROOTS = RiskContext(
    allowed_roots=(r"C:\Users\serik\Desktop", r"C:\Users\serik\Documents"),
    executable_roots=(r"C:\Program Files", r"C:\Windows\System32"),
)


class DemoArgs(BaseModel):
    flag: bool = False


def tool(risk: RiskLevel, *rules: RiskRule, name: str = "demo.tool") -> ToolManifest:
    return ToolManifest(
        name=name,
        version=1,
        summary="fixture",
        args_model=DemoArgs,
        base_risk=risk,
        reversible=True,
        escalations=rules,
        requires_capabilities=("demo",),
    )


def request(
    manifest: ToolManifest,
    *,
    args: dict[str, Any] | None = None,
    trust: TrustLevel = TrustLevel.TRUSTED,
    mode: AgentMode = AgentMode.NORMAL,
    overrides: tuple[PermissionOverride, ...] = (),
) -> PolicyRequest:
    return PolicyRequest(
        tool=manifest,
        args=args or {},
        risk_context=ROOTS,
        device_trust=trust,
        agent_mode=mode,
        now=NOW,
        overrides=overrides,
    )


class TestBaselineMapping:
    def test_low_risk_is_allowed(self) -> None:
        assert decide(request(tool(RiskLevel.LOW))).decision is Decision.ALLOW

    def test_medium_risk_requires_confirmation(self) -> None:
        result = decide(request(tool(RiskLevel.MEDIUM)))
        assert result.decision is Decision.CONFIRM
        assert "medium risk requires confirmation" in result.reasons

    def test_high_risk_requires_confirmation(self) -> None:
        result = decide(request(tool(RiskLevel.HIGH)))
        assert result.decision is Decision.CONFIRM

    def test_escalated_risk_drives_the_decision(self) -> None:
        escalating = tool(
            RiskLevel.LOW,
            RiskRule(
                to=RiskLevel.HIGH,
                reason="flag makes this dangerous",
                conditions=(Condition(field="flag", op=ConditionOp.IS_TRUE, default=False),),
            ),
        )
        assert decide(request(escalating, args={"flag": False})).decision is Decision.ALLOW

        escalated = decide(request(escalating, args={"flag": True}))
        assert escalated.decision is Decision.CONFIRM
        assert escalated.risk is RiskLevel.HIGH
        assert "flag makes this dangerous" in escalated.reasons


class TestForbidden:
    def test_deny_risk_is_refused_outright(self) -> None:
        forbidden = tool(
            RiskLevel.LOW,
            RiskRule(
                to=RiskLevel.DENY,
                reason="structurally forbidden",
                conditions=(Condition(field="flag", op=ConditionOp.IS_TRUE, default=False),),
            ),
        )
        result = decide(request(forbidden, args={"flag": True}))
        assert result.decision is Decision.DENY
        assert result.risk is RiskLevel.DENY

    def test_deny_cannot_be_lifted_by_an_override(self) -> None:
        # This is what separates DENY from HIGH: no confirmation path exists.
        forbidden = tool(
            RiskLevel.LOW,
            RiskRule(
                to=RiskLevel.DENY,
                reason="structurally forbidden",
                conditions=(Condition(field="flag", op=ConditionOp.IS_TRUE, default=False),),
            ),
        )
        result = decide(
            request(
                forbidden,
                args={"flag": True},
                overrides=(PermissionOverride("demo.tool", OverrideMode.ALWAYS_ALLOW),),
            )
        )
        assert result.decision is Decision.DENY

    def test_unevaluable_rule_denies(self) -> None:
        # A safety rule that cannot be evaluated must stop the action, not be
        # skipped as though it had passed.
        broken = tool(
            RiskLevel.LOW,
            RiskRule(
                to=RiskLevel.HIGH,
                reason="needs an argument nobody supplied",
                conditions=(Condition(field="absent", op=ConditionOp.IS_TRUE),),
            ),
        )
        result = decide(request(broken))
        assert result.decision is Decision.DENY
        assert "risk could not be evaluated" in result.reasons[0]


class TestDeviceTrust:
    def test_limited_device_may_read(self) -> None:
        assert (
            decide(request(tool(RiskLevel.LOW), trust=TrustLevel.LIMITED)).decision
            is Decision.ALLOW
        )

    @pytest.mark.parametrize("risk", [RiskLevel.MEDIUM, RiskLevel.HIGH])
    def test_limited_device_may_not_act(self, risk: RiskLevel) -> None:
        result = decide(request(tool(risk), trust=TrustLevel.LIMITED))
        assert result.decision is Decision.DENY
        assert any("device trust" in reason for reason in result.reasons)

    def test_limited_device_cannot_be_promoted_by_an_override(self) -> None:
        result = decide(
            request(
                tool(RiskLevel.MEDIUM),
                trust=TrustLevel.LIMITED,
                overrides=(PermissionOverride("demo.tool", OverrideMode.ALWAYS_ALLOW),),
            )
        )
        assert result.decision is Decision.DENY


class TestSafeMode:
    def test_low_risk_still_runs(self) -> None:
        result = decide(request(tool(RiskLevel.LOW), mode=AgentMode.SAFE))
        assert result.decision is Decision.ALLOW

    @pytest.mark.parametrize("risk", [RiskLevel.MEDIUM, RiskLevel.HIGH])
    def test_anything_above_low_is_denied(self, risk: RiskLevel) -> None:
        result = decide(request(tool(risk), mode=AgentMode.SAFE))
        assert result.decision is Decision.DENY
        assert any("SAFE MODE" in reason for reason in result.reasons)

    def test_an_override_cannot_bypass_safe_mode(self) -> None:
        # Nothing configured in advance may re-enable what SAFE MODE turned off.
        result = decide(
            request(
                tool(RiskLevel.MEDIUM),
                mode=AgentMode.SAFE,
                overrides=(PermissionOverride("demo.tool", OverrideMode.ALWAYS_ALLOW),),
            )
        )
        assert result.decision is Decision.DENY


class TestOverrides:
    def test_always_allow_relaxes_medium(self) -> None:
        result = decide(
            request(
                tool(RiskLevel.MEDIUM),
                overrides=(PermissionOverride("demo.tool", OverrideMode.ALWAYS_ALLOW),),
            )
        )
        assert result.decision is Decision.ALLOW
        assert "pre-authorised by a user permission rule" in result.reasons

    def test_always_allow_does_not_relax_high(self) -> None:
        result = decide(
            request(
                tool(RiskLevel.HIGH),
                overrides=(PermissionOverride("demo.tool", OverrideMode.ALWAYS_ALLOW),),
            )
        )
        assert result.decision is Decision.CONFIRM

    def test_always_confirm_tightens_low(self) -> None:
        result = decide(
            request(
                tool(RiskLevel.LOW),
                overrides=(PermissionOverride("demo.tool", OverrideMode.ALWAYS_CONFIRM),),
            )
        )
        assert result.decision is Decision.CONFIRM

    def test_deny_override_beats_always_allow(self) -> None:
        result = decide(
            request(
                tool(RiskLevel.LOW),
                overrides=(
                    PermissionOverride("demo.tool", OverrideMode.ALWAYS_ALLOW),
                    PermissionOverride("demo.tool", OverrideMode.DENY),
                ),
            )
        )
        assert result.decision is Decision.DENY

    def test_expired_override_is_ignored(self) -> None:
        expired = PermissionOverride(
            "demo.tool", OverrideMode.ALWAYS_ALLOW, expires_at=NOW - timedelta(seconds=1)
        )
        assert decide(request(tool(RiskLevel.MEDIUM), overrides=(expired,))).decision is (
            Decision.CONFIRM
        )

    def test_override_expiring_later_still_applies(self) -> None:
        live = PermissionOverride(
            "demo.tool", OverrideMode.ALWAYS_ALLOW, expires_at=NOW + timedelta(hours=1)
        )
        assert decide(request(tool(RiskLevel.MEDIUM), overrides=(live,))).decision is (
            Decision.ALLOW
        )

    def test_override_for_another_tool_is_ignored(self) -> None:
        other = PermissionOverride("something.else", OverrideMode.ALWAYS_ALLOW)
        assert decide(request(tool(RiskLevel.MEDIUM), overrides=(other,))).decision is (
            Decision.CONFIRM
        )

    @pytest.mark.parametrize(
        ("pattern", "tool_name", "expected"),
        [
            ("fs.*", "fs.delete", True),
            ("fs.*", "fs.open", True),
            ("fs.*", "app.launch", False),
            ("fs.delete", "fs.delete", True),
            ("fs.delete", "fs.deletion", False),
        ],
    )
    def test_pattern_matching(self, pattern: str, tool_name: str, expected: bool) -> None:
        assert PermissionOverride(pattern, OverrideMode.DENY).matches(tool_name) is expected


class TestRealCatalogue:
    """The declared tools must land where the risk table says they do."""

    def test_reading_metrics_is_allowed(self) -> None:
        result = decide(request(CATALOG.get("system.metrics")))
        assert result.decision is Decision.ALLOW

    def test_launching_a_known_application_is_allowed(self) -> None:
        result = decide(request(CATALOG.get("app.launch"), args={"name": "chrome"}))
        assert result.decision is Decision.ALLOW

    def test_launching_an_unknown_binary_needs_confirmation(self) -> None:
        result = decide(
            request(
                CATALOG.get("app.launch"),
                args={"name": "x", "executable_path": r"C:\Users\serik\Downloads\x.exe"},
            )
        )
        assert result.decision is Decision.CONFIRM
        assert result.risk is RiskLevel.HIGH

    def test_deleting_inside_the_roots_needs_confirmation(self) -> None:
        result = decide(
            request(
                CATALOG.get("fs.delete"),
                args={"paths": (r"C:\Users\serik\Desktop\a.txt",), "recursive": False},
            )
        )
        assert result.decision is Decision.CONFIRM
        assert result.risk is RiskLevel.MEDIUM

    def test_deleting_outside_the_roots_is_forbidden(self) -> None:
        result = decide(
            request(
                CATALOG.get("fs.delete"),
                args={"paths": (r"C:\Windows\System32\drivers\etc\hosts",), "recursive": False},
            )
        )
        assert result.decision is Decision.DENY

    def test_path_traversal_is_forbidden(self) -> None:
        result = decide(
            request(
                CATALOG.get("fs.open"),
                args={"path": r"C:\Users\serik\Desktop\..\.ssh\id_ed25519"},
            )
        )
        assert result.decision is Decision.DENY

    def test_a_standing_allow_cannot_pre_authorise_bulk_deletion(self) -> None:
        paths = tuple(rf"C:\Users\serik\Desktop\f{n}.txt" for n in range(30))
        result = decide(
            request(
                CATALOG.get("fs.delete"),
                args={"paths": paths, "recursive": False},
                overrides=(PermissionOverride("fs.*", OverrideMode.ALWAYS_ALLOW),),
            )
        )
        assert result.decision is Decision.CONFIRM
        assert result.risk is RiskLevel.HIGH


def test_decision_is_deterministic() -> None:
    manifest = CATALOG.get("fs.delete")
    args = {"paths": (r"C:\Users\serik\Desktop\a.txt",), "recursive": True}
    first = decide(request(manifest, args=args))
    for _ in range(50):
        assert decide(request(manifest, args=args)) == first
