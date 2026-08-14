"""Shared contracts for every ATLAS component.

This package has no side effects, no I/O and no framework dependencies. Both the
backend and the Windows agent import it, which is what keeps their view of the
protocol, the risk model and the tool catalogue identical by construction.
"""

from atlas_shared.canonical import canonical_json, canonical_sha256_hex
from atlas_shared.enums import (
    AgentMode,
    CaptureScope,
    Decision,
    DeviceKind,
    Language,
    MessageKind,
    Priority,
    RefusalReason,
    RiskLevel,
    ToolStatus,
    TrustLevel,
)
from atlas_shared.ids import is_ulid, new_ulid, ulid_timestamp
from atlas_shared.protocol import PROTOCOL_VERSION

__version__ = "0.1.0"

__all__ = [
    "PROTOCOL_VERSION",
    "AgentMode",
    "CaptureScope",
    "Decision",
    "DeviceKind",
    "Language",
    "MessageKind",
    "Priority",
    "RefusalReason",
    "RiskLevel",
    "ToolStatus",
    "TrustLevel",
    "__version__",
    "canonical_json",
    "canonical_sha256_hex",
    "is_ulid",
    "new_ulid",
    "ulid_timestamp",
]
