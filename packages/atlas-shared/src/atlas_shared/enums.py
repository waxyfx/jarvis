"""Shared enumerations.

These values travel across the wire and are persisted in the database, so their
string forms are part of the public contract. Renaming a member is a breaking
change and requires a protocol version bump plus a data migration.
"""

from __future__ import annotations

from enum import StrEnum


class MessageKind(StrEnum):
    """Envelope category. See docs/protocol.md."""

    CMD = "cmd"
    RES = "res"
    EVT = "evt"
    ERR = "err"


class DeviceKind(StrEnum):
    WINDOWS_AGENT = "windows_agent"
    IOS = "ios"
    WEB = "web"


class TrustLevel(StrEnum):
    #: Full participant: may receive commands and confirm MEDIUM/HIGH actions.
    TRUSTED = "trusted"
    #: Paired but restricted: read-only surfaces, cannot confirm risky actions.
    LIMITED = "limited"
    #: Terminal state. A revoked device can never be un-revoked; re-pair instead.
    REVOKED = "revoked"


class AgentMode(StrEnum):
    NORMAL = "normal"
    #: See docs/VISION-POLICY.md §3. Only safe local reads; cloud vision and all
    #: MEDIUM/HIGH actions are refused, and user confirmation cannot lift that.
    SAFE = "safe"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    #: Not a risk tier but a verdict: structurally forbidden, no confirmation path.
    DENY = "deny"

    @property
    def rank(self) -> int:
        return _RISK_RANK[self]

    def escalated_to(self, other: RiskLevel) -> RiskLevel:
        """Risk only ever moves upward during evaluation."""
        return other if other.rank > self.rank else self


_RISK_RANK: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.DENY: 3,
}


class Decision(StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


class Language(StrEnum):
    RU = "ru"
    EN = "en"
    KK = "kk"


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    IMPORTANT = "important"
    CRITICAL = "critical"


class CaptureScope(StrEnum):
    """Screen capture breadth, narrowest first (VISION-POLICY.md R2)."""

    ELEMENT = "element"
    WINDOW = "window"
    MONITOR = "monitor"
    DESKTOP = "desktop"
