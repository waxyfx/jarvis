"""Protocol-level error codes.

These codes cross the wire and are logged; treat them as a stable contract.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

__all__ = ["AtlasProtocolError", "ErrorCode"]


class ErrorCode(StrEnum):
    #: Envelope could not be parsed, or failed schema validation.
    MALFORMED = "malformed"
    #: Major protocol version mismatch — the connection must be closed.
    UNSUPPORTED_VERSION = "unsupported_version"
    #: Well-formed envelope naming a message type this peer does not know.
    UNSUPPORTED_TYPE = "unsupported_type"
    #: Message kind is not permitted for this message type.
    INVALID_KIND = "invalid_kind"
    #: No credentials, or credentials are expired/unknown.
    UNAUTHORIZED = "unauthorized"
    #: Authenticated, but not permitted to do this.
    FORBIDDEN = "forbidden"
    #: Envelope signature missing or did not verify.
    SIGNATURE_INVALID = "signature_invalid"
    #: Message id or nonce has been seen before.
    REPLAY_DETECTED = "replay_detected"
    #: Peer did not answer a cmd within its deadline.
    TIMEOUT = "timeout"
    #: Too many requests for this tool or connection.
    RATE_LIMITED = "rate_limited"
    #: Agent is in SAFE MODE; see docs/VISION-POLICY.md §3.
    SAFE_MODE = "safe_mode"
    #: Manifest exists but no executor is bound (expected before M2 lands).
    TOOL_NOT_IMPLEMENTED = "tool_not_implemented"
    #: Unhandled server-side failure.
    INTERNAL = "internal"


class AtlasProtocolError(Exception):
    """Raised for any condition that must be reported as an ``err`` envelope."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}
