from typing import Any

import pytest
from pydantic import BaseModel

from atlas_shared.enums import RiskLevel
from atlas_shared.tools.manifest import (
    Condition,
    ConditionOp,
    ManifestEvaluationError,
    RiskContext,
    RiskRule,
    ToolManifest,
)

ROOTS = RiskContext(
    allowed_roots=(r"C:\Users\serik\Desktop", r"C:\Users\serik\Documents"),
    executable_roots=(r"C:\Program Files", r"C:\Windows\System32"),
)


class DemoArgs(BaseModel):
    paths: tuple[str, ...] = ()
    recursive: bool = False
    count: int = 0
    label: str = ""


def manifest(*rules: RiskRule, base: RiskLevel = RiskLevel.LOW) -> ToolManifest:
    return ToolManifest(
        name="demo.tool",
        version=1,
        summary="fixture",
        args_model=DemoArgs,
        base_risk=base,
        reversible=True,
        escalations=rules,
    )


class TestConditions:
    @pytest.mark.parametrize(
        ("op", "value", "actual", "expected"),
        [
            (ConditionOp.EQ, 5, 5, True),
            (ConditionOp.EQ, 5, 6, False),
            (ConditionOp.NE, 5, 6, True),
            (ConditionOp.GT, 10, 11, True),
            (ConditionOp.GT, 10, 10, False),
            (ConditionOp.GTE, 10, 10, True),
            (ConditionOp.LT, 10, 9, True),
            (ConditionOp.LTE, 10, 10, True),
        ],
    )
    def test_numeric_operators(
        self, op: ConditionOp, value: int, actual: int, expected: bool
    ) -> None:
        condition = Condition(field="count", op=op, value=value)
        assert condition.evaluate({"count": actual}, ROOTS) is expected

    def test_membership_operators(self) -> None:
        inside = Condition(field="label", op=ConditionOp.IN, value=["a", "b"])
        assert inside.evaluate({"label": "a"}, ROOTS)
        assert not inside.evaluate({"label": "c"}, ROOTS)

        outside = Condition(field="label", op=ConditionOp.NOT_IN, value=["a", "b"])
        assert outside.evaluate({"label": "c"}, ROOTS)

    def test_boolean_operators(self) -> None:
        assert Condition(field="recursive", op=ConditionOp.IS_TRUE).evaluate(
            {"recursive": True}, ROOTS
        )
        assert Condition(field="recursive", op=ConditionOp.IS_FALSE).evaluate(
            {"recursive": False}, ROOTS
        )

    def test_regex_operator_is_case_insensitive(self) -> None:
        condition = Condition(field="label", op=ConditionOp.MATCHES, value=r"\.exe$")
        assert condition.evaluate({"label": "setup.EXE"}, ROOTS)
        assert not condition.evaluate({"label": "notes.txt"}, ROOTS)

    def test_length_operators(self) -> None:
        greater = Condition(field="paths", op=ConditionOp.LENGTH_GT, value=2)
        assert greater.evaluate({"paths": ["a", "b", "c"]}, ROOTS)
        assert not greater.evaluate({"paths": ["a", "b"]}, ROOTS)

        at_least = Condition(field="paths", op=ConditionOp.LENGTH_GTE, value=2)
        assert at_least.evaluate({"paths": ["a", "b"]}, ROOTS)

    def test_length_operator_rejects_strings(self) -> None:
        # len("abc") > 2 would be true, which is never what the rule author meant.
        condition = Condition(field="label", op=ConditionOp.LENGTH_GT, value=2)
        with pytest.raises(ManifestEvaluationError, match="must be a sequence"):
            condition.evaluate({"label": "abc"}, ROOTS)

    def test_nested_field_lookup(self) -> None:
        condition = Condition(field="outer.inner", op=ConditionOp.EQ, value=1)
        assert condition.evaluate({"outer": {"inner": 1}}, ROOTS)


class TestMissingArguments:
    def test_absent_field_without_default_raises(self) -> None:
        # Fail loud: a safety rule that cannot be evaluated must not be skipped.
        condition = Condition(field="recursive", op=ConditionOp.IS_TRUE)
        with pytest.raises(ManifestEvaluationError, match="declares no default"):
            condition.evaluate({}, ROOTS)

    def test_absent_field_with_default_uses_it(self) -> None:
        condition = Condition(field="recursive", op=ConditionOp.IS_TRUE, default=False)
        assert condition.evaluate({}, ROOTS) is False

    def test_unevaluable_rule_propagates_out_of_assess(self) -> None:
        tool = manifest(
            RiskRule(
                to=RiskLevel.HIGH,
                reason="needs an argument that is not there",
                conditions=(Condition(field="missing", op=ConditionOp.IS_TRUE),),
            )
        )
        with pytest.raises(ManifestEvaluationError):
            tool.assess({})


class TestPathRules:
    @pytest.mark.parametrize(
        ("path", "outside"),
        [
            (r"C:\Users\serik\Desktop\a.txt", False),
            (r"C:/Users/serik/Desktop/a.txt", False),
            (r"c:\users\serik\desktop\a.txt", False),
            (r"C:\Users\serik\Desktop", False),
            (r"C:\Windows\System32\config", True),
            (r"C:\Users\serik\Desktop\..\.ssh\id_ed25519", True),
            (r"C:\Users\serik\DesktopEvil\a.txt", True),
        ],
    )
    def test_single_path(self, path: str, outside: bool) -> None:
        condition = Condition(field="p", op=ConditionOp.PATH_OUTSIDE_ROOTS)
        assert condition.evaluate({"p": path}, ROOTS) is outside

    def test_sequence_flags_when_any_element_escapes(self) -> None:
        condition = Condition(field="paths", op=ConditionOp.PATH_OUTSIDE_ROOTS)
        allowed = [r"C:\Users\serik\Desktop\a", r"C:\Users\serik\Documents\b"]
        assert not condition.evaluate({"paths": allowed}, ROOTS)
        assert condition.evaluate({"paths": [*allowed, r"C:\Windows\x"]}, ROOTS)

    def test_executable_roots_are_a_separate_set(self) -> None:
        condition = Condition(field="p", op=ConditionOp.PATH_OUTSIDE_ROOTS, roots="executables")
        assert not condition.evaluate({"p": r"C:\Program Files\App\app.exe"}, ROOTS)
        assert condition.evaluate({"p": r"C:\Users\serik\Downloads\thing.exe"}, ROOTS)

    def test_empty_roots_raise_rather_than_allow(self) -> None:
        condition = Condition(field="p", op=ConditionOp.PATH_OUTSIDE_ROOTS)
        with pytest.raises(ManifestEvaluationError, match="non-empty"):
            condition.evaluate({"p": "anything"}, RiskContext())

    def test_unknown_roots_selector_raises(self) -> None:
        condition = Condition(field="p", op=ConditionOp.PATH_OUTSIDE_ROOTS, roots="nonsense")
        with pytest.raises(ManifestEvaluationError, match="unknown roots selector"):
            condition.evaluate({"p": "x"}, ROOTS)


class TestAssessment:
    def test_base_risk_when_no_rule_fires(self) -> None:
        result = manifest(base=RiskLevel.MEDIUM).assess({})
        assert result.level is RiskLevel.MEDIUM
        assert result.applied_rules == ()

    def test_rule_escalates_and_is_reported(self) -> None:
        tool = manifest(
            RiskRule(
                to=RiskLevel.HIGH,
                reason="recursive",
                conditions=(Condition(field="recursive", op=ConditionOp.IS_TRUE, default=False),),
            )
        )
        result = tool.assess({"recursive": True})
        assert result.level is RiskLevel.HIGH
        assert result.applied_rules == ("recursive",)

    def test_risk_never_moves_down(self) -> None:
        # A rule declaring a lower tier must not weaken a tool. Risk is a ratchet.
        tool = manifest(
            RiskRule(
                to=RiskLevel.LOW,
                reason="attempted downgrade",
                conditions=(Condition(field="recursive", op=ConditionOp.IS_FALSE, default=False),),
            ),
            base=RiskLevel.HIGH,
        )
        result = tool.assess({"recursive": False})
        assert result.level is RiskLevel.HIGH
        assert result.applied_rules == ()

    def test_highest_matching_rule_wins(self) -> None:
        tool = manifest(
            RiskRule(
                to=RiskLevel.MEDIUM,
                reason="medium",
                conditions=(Condition(field="count", op=ConditionOp.GT, value=1),),
            ),
            RiskRule(
                to=RiskLevel.DENY,
                reason="deny",
                conditions=(Condition(field="count", op=ConditionOp.GT, value=100),),
            ),
        )
        assert tool.assess({"count": 500}).level is RiskLevel.DENY
        assert tool.assess({"count": 5}).level is RiskLevel.MEDIUM

    def test_rule_order_does_not_change_the_outcome(self) -> None:
        high = RiskRule(
            to=RiskLevel.HIGH,
            reason="high",
            conditions=(Condition(field="count", op=ConditionOp.GT, value=1),),
        )
        medium = RiskRule(
            to=RiskLevel.MEDIUM,
            reason="medium",
            conditions=(Condition(field="count", op=ConditionOp.GT, value=1),),
        )
        assert manifest(high, medium).assess({"count": 2}).level is RiskLevel.HIGH
        assert manifest(medium, high).assess({"count": 2}).level is RiskLevel.HIGH

    def test_match_any_semantics(self) -> None:
        rule = RiskRule(
            to=RiskLevel.HIGH,
            reason="either condition suffices",
            match="any",
            conditions=(
                Condition(field="recursive", op=ConditionOp.IS_TRUE, default=False),
                Condition(field="count", op=ConditionOp.GT, value=10, default=0),
            ),
        )
        tool = manifest(rule)
        assert tool.assess({"recursive": True, "count": 0}).level is RiskLevel.HIGH
        assert tool.assess({"recursive": False, "count": 50}).level is RiskLevel.HIGH
        assert tool.assess({"recursive": False, "count": 1}).level is RiskLevel.LOW

    def test_match_all_semantics(self) -> None:
        rule = RiskRule(
            to=RiskLevel.HIGH,
            reason="both conditions required",
            match="all",
            conditions=(
                Condition(field="recursive", op=ConditionOp.IS_TRUE, default=False),
                Condition(field="count", op=ConditionOp.GT, value=10, default=0),
            ),
        )
        tool = manifest(rule)
        assert tool.assess({"recursive": True, "count": 50}).level is RiskLevel.HIGH
        assert tool.assess({"recursive": True, "count": 1}).level is RiskLevel.LOW


class TestManifestSurface:
    def test_validate_args_accepts_good_input(self) -> None:
        parsed = manifest().validate_args({"count": 3, "recursive": True})
        assert isinstance(parsed, DemoArgs)
        assert parsed.count == 3

    def test_validate_args_rejects_bad_input(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            manifest().validate_args({"count": "not a number"})

    def test_descriptor_is_json_serialisable(self) -> None:
        descriptor = manifest().to_descriptor()
        dumped: dict[str, Any] = descriptor.model_dump(mode="json")
        assert dumped["name"] == "demo.tool"
        assert "properties" in dumped["args_schema"]
