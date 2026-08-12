"""Tool manifests: the single source of truth for what ATLAS may do.

One declaration feeds three consumers:

* the LLM, as a function declaration generated from ``args_model``;
* the Policy Engine, which reads ``base_risk`` and ``escalations``;
* the agent executor, which validates arguments against the same model.

Because all three read the same object, "the model knows about a tool the policy
does not" is not a state this system can reach.

Escalation conditions are *structured data*, never expression strings. There is
no ``eval`` anywhere in the risk path: a malformed rule fails loudly at import
time instead of quietly granting permission at runtime.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from atlas_shared.enums import RiskLevel

__all__ = [
    "Condition",
    "ConditionOp",
    "ManifestEvaluationError",
    "RiskAssessment",
    "RiskContext",
    "RiskRule",
    "ToolDescriptor",
    "ToolManifest",
]

_MISSING = object()


class ManifestEvaluationError(Exception):
    """A risk rule could not be evaluated.

    Callers must treat this as DENY. An unevaluable safety rule is a failure of
    the safety system, and the only safe reading of a broken guard is "stop".
    """


class ConditionOp(StrEnum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"
    IS_TRUE = "is_true"
    IS_FALSE = "is_false"
    MATCHES = "matches"
    #: Length of a sequence argument exceeds a threshold. Used for bulk-operation
    #: escalation, where the count of targets is what makes an action dangerous.
    LENGTH_GT = "length_gt"
    LENGTH_GTE = "length_gte"
    #: Path argument — a string, or any element of a sequence — resolves outside
    #: every allowed root. Evaluated here as a cheap pre-filter only; the agent
    #: re-checks with real symlink resolution, because the server cannot see the
    #: target machine's filesystem and must never be the sole path authority.
    PATH_OUTSIDE_ROOTS = "path_outside_roots"


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Ambient facts a rule may consult, beyond the tool arguments."""

    #: Roots under which file operations are permitted.
    allowed_roots: tuple[str, ...] = ()
    #: Roots from which executables are considered known-good. An executable
    #: outside these is "unknown" and escalates to HIGH.
    executable_roots: tuple[str, ...] = ()

    def roots_for(self, selector: str) -> tuple[str, ...]:
        match selector:
            case "files":
                return self.allowed_roots
            case "executables":
                return self.executable_roots
        raise ManifestEvaluationError(f"unknown roots selector: {selector!r}")


@dataclass(frozen=True, slots=True)
class Condition:
    """A single predicate over one tool argument."""

    field: str
    op: ConditionOp
    value: Any = None
    #: Value assumed when the argument is absent. Without an explicit default,
    #: a missing argument raises rather than silently skipping the rule.
    default: Any = _MISSING
    #: Which root set :attr:`ConditionOp.PATH_OUTSIDE_ROOTS` consults.
    roots: str = "files"

    def evaluate(self, args: Mapping[str, Any], context: RiskContext) -> bool:
        actual = self._resolve(args)

        match self.op:
            case ConditionOp.EQ:
                return bool(actual == self.value)
            case ConditionOp.NE:
                return bool(actual != self.value)
            case ConditionOp.GT | ConditionOp.GTE | ConditionOp.LT | ConditionOp.LTE:
                return self._compare(actual)
            case ConditionOp.IN:
                return actual in self._as_collection()
            case ConditionOp.NOT_IN:
                return actual not in self._as_collection()
            case ConditionOp.IS_TRUE:
                return bool(actual)
            case ConditionOp.IS_FALSE:
                return not bool(actual)
            case ConditionOp.MATCHES:
                return self._matches(actual)
            case ConditionOp.LENGTH_GT | ConditionOp.LENGTH_GTE:
                return self._length_compare(actual)
            case ConditionOp.PATH_OUTSIDE_ROOTS:
                return self._path_outside_roots(actual, context)

        raise ManifestEvaluationError(f"unhandled operator {self.op}")  # pragma: no cover

    def _resolve(self, args: Mapping[str, Any]) -> Any:
        current: Any = args
        for part in self.field.split("."):
            if isinstance(current, Mapping) and part in current:
                current = current[part]
                continue
            if self.default is _MISSING:
                raise ManifestEvaluationError(
                    f"argument {self.field!r} is absent and the rule declares no default"
                )
            return self.default
        return current

    def _compare(self, actual: Any) -> bool:
        if not isinstance(actual, int | float) or isinstance(actual, bool):
            raise ManifestEvaluationError(
                f"{self.field!r} must be numeric for {self.op}, got {type(actual).__name__}"
            )
        if not isinstance(self.value, int | float):
            raise ManifestEvaluationError(f"{self.op} requires a numeric threshold")
        match self.op:
            case ConditionOp.GT:
                return actual > self.value
            case ConditionOp.GTE:
                return actual >= self.value
            case ConditionOp.LT:
                return actual < self.value
            case _:
                return actual <= self.value

    def _as_collection(self) -> tuple[Any, ...]:
        if not isinstance(self.value, Sequence) or isinstance(self.value, str | bytes):
            raise ManifestEvaluationError(f"{self.op} requires a sequence value")
        return tuple(self.value)

    def _matches(self, actual: Any) -> bool:
        if not isinstance(actual, str):
            raise ManifestEvaluationError(f"{self.field!r} must be a string for {self.op}")
        if not isinstance(self.value, str):
            raise ManifestEvaluationError(f"{self.op} requires a regex string")
        return re.search(self.value, actual, re.IGNORECASE) is not None

    def _length_compare(self, actual: Any) -> bool:
        if isinstance(actual, str | bytes) or not isinstance(actual, Sequence | Mapping):
            raise ManifestEvaluationError(
                f"{self.field!r} must be a sequence for {self.op}, got {type(actual).__name__}"
            )
        if not isinstance(self.value, int):
            raise ManifestEvaluationError(f"{self.op} requires an integer threshold")
        length = len(actual)
        return length > self.value if self.op is ConditionOp.LENGTH_GT else length >= self.value

    def _path_outside_roots(self, actual: Any, context: RiskContext) -> bool:
        if actual is None:
            # No path supplied, so there is nothing outside the roots. Callers
            # express "absent" with an explicit default=None on the condition.
            return False
        if isinstance(actual, str):
            candidates = [actual]
        elif isinstance(actual, Sequence) and not isinstance(actual, bytes):
            candidates = list(actual)
        else:
            raise ManifestEvaluationError(
                f"{self.field!r} must be a path or sequence of paths for {self.op}"
            )

        roots = context.roots_for(self.roots)
        if not roots:
            raise ManifestEvaluationError(
                f"{self.op} requires non-empty {self.roots} roots in the risk context"
            )

        normalised_roots = [_normalise_path(root).rstrip("/") for root in roots]
        for candidate in candidates:
            if not isinstance(candidate, str):
                raise ManifestEvaluationError(f"{self.field!r} contains a non-string path")
            path = _normalise_path(candidate)
            inside = any(path == root or path.startswith(root + "/") for root in normalised_roots)
            if not inside:
                return True
        return False


def _normalise_path(value: str) -> str:
    """Lowercase, forward-slashed, ``..``-free path for prefix comparison.

    Deliberately textual: this runs on the server, which has no access to the
    agent's filesystem. It exists to catch obvious escapes early, not to be the
    authoritative check.
    """
    text = value.replace("\\", "/").strip()
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    prefix = "/" if text.startswith("/") else ""
    return (prefix + "/".join(parts)).lower()


@dataclass(frozen=True, slots=True)
class RiskRule:
    """Raises a tool's risk when its conditions hold."""

    to: RiskLevel
    reason: str
    conditions: tuple[Condition, ...]
    #: ``all`` = every condition must hold; ``any`` = at least one.
    match: str = "all"

    def applies(self, args: Mapping[str, Any], context: RiskContext) -> bool:
        results = (condition.evaluate(args, context) for condition in self.conditions)
        return all(results) if self.match == "all" else any(results)


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    level: RiskLevel
    applied_rules: tuple[str, ...] = ()


class ToolDescriptor(BaseModel):
    """Serialisable projection of a manifest, for the wire and for the LLM."""

    name: str
    version: int
    summary: str
    args_schema: dict[str, Any]
    base_risk: RiskLevel
    reversible: bool
    requires_capabilities: tuple[str, ...]
    side_effects: tuple[str, ...]
    timeout_s: float


@dataclass(frozen=True, slots=True)
class ToolManifest:
    """Declaration of one capability ATLAS can exercise."""

    name: str
    version: int
    summary: str
    args_model: type[BaseModel]
    base_risk: RiskLevel
    reversible: bool
    timeout_s: float = 30.0
    escalations: tuple[RiskRule, ...] = ()
    requires_capabilities: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    rate_limit_per_minute: int | None = None

    def assess(self, args: Mapping[str, Any], context: RiskContext | None = None) -> RiskAssessment:
        """Compute the effective risk of invoking this tool with ``args``.

        Risk only ever moves upward: no rule can lower a tool below its base.
        """
        ctx = context or RiskContext()
        level = self.base_risk
        applied: list[str] = []

        for rule in self.escalations:
            if rule.applies(args, ctx):
                new_level = level.escalated_to(rule.to)
                if new_level is not level:
                    applied.append(rule.reason)
                    level = new_level

        return RiskAssessment(level=level, applied_rules=tuple(applied))

    def validate_args(self, args: Mapping[str, Any]) -> BaseModel:
        """Validate raw arguments against the declared model."""
        return self.args_model.model_validate(dict(args))

    def to_descriptor(self) -> ToolDescriptor:
        return ToolDescriptor(
            name=self.name,
            version=self.version,
            summary=self.summary,
            args_schema=self.args_model.model_json_schema(),
            base_risk=self.base_risk,
            reversible=self.reversible,
            requires_capabilities=self.requires_capabilities,
            side_effects=self.side_effects,
            timeout_s=self.timeout_s,
        )
