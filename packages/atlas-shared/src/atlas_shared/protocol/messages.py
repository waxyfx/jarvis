"""Message registry: the exhaustive list of types this protocol version knows.

A message type is only valid if it is registered here together with its payload
model and the envelope kinds it may appear as. Parsing goes through
:func:`parse_message`, so an unregistered type can never reach business logic —
which is what stops a peer from inventing message types.

M1 defines the connection lifecycle only. Tool execution, voice and media types
arrive with their phases (M2+) and register themselves the same way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from atlas_shared.enums import (
    AgentMode,
    DeviceKind,
    MessageKind,
    RefusalReason,
    RiskLevel,
    ToolStatus,
)
from atlas_shared.ids import new_ulid
from atlas_shared.protocol.envelope import PROTOCOL_VERSION, Envelope, verify_envelope
from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode

__all__ = [
    "ActivityBatch",
    "ActivitySample",
    "AgentHello",
    "ClientHello",
    "ConnPing",
    "ConnPong",
    "DiskUsage",
    "EnterSafeMode",
    "ErrorPayload",
    "HelloAck",
    "MessageSpec",
    "ModeChanged",
    "ParsedMessage",
    "SystemTelemetry",
    "ToolCancel",
    "ToolExecute",
    "ToolFailure",
    "ToolResult",
    "build_envelope",
    "known_types",
    "parse_message",
    "register",
    "require_signature",
    "spec_for",
]


class _Payload(BaseModel):
    """Base for every payload model: unknown fields are an error, not noise."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True, slots=True)
class MessageSpec:
    # Named ``message_type`` rather than ``type``: a field called ``type`` would
    # shadow the builtin inside the class body, breaking the annotation below.
    message_type: str
    payload_model: type[_Payload]
    kinds: frozenset[MessageKind]
    #: True when the receiver must verify an envelope signature before acting.
    signature_required: bool


_REGISTRY: dict[str, MessageSpec] = {}

PayloadT = TypeVar("PayloadT", bound=_Payload)


def register(
    type_name: str,
    *kinds: MessageKind,
    signature_required: bool = False,
) -> Any:
    """Class decorator binding a payload model to a wire type."""

    def decorator(model: type[PayloadT]) -> type[PayloadT]:
        if type_name in _REGISTRY:
            raise RuntimeError(f"duplicate message type registration: {type_name}")
        if not kinds:
            raise RuntimeError(f"{type_name}: at least one MessageKind is required")
        _REGISTRY[type_name] = MessageSpec(
            message_type=type_name,
            payload_model=model,
            kinds=frozenset(kinds),
            signature_required=signature_required,
        )
        return model

    return decorator


def spec_for(type_name: str) -> MessageSpec:
    spec = _REGISTRY.get(type_name)
    if spec is None:
        raise AtlasProtocolError(
            ErrorCode.UNSUPPORTED_TYPE,
            f"unknown message type: {type_name}",
            {"type": type_name},
        )
    return spec


def known_types() -> frozenset[str]:
    return frozenset(_REGISTRY)


# --------------------------------------------------------------------------
# Connection lifecycle (M1)
# --------------------------------------------------------------------------


@register("agent.hello", MessageKind.CMD)
class AgentHello(_Payload):
    """First message from a Windows Agent after the socket opens."""

    agent_version: str
    protocol_version: int
    platform: str
    hostname: str
    mode: AgentMode
    capabilities: tuple[str, ...] = ()


@register("client.hello", MessageKind.CMD)
class ClientHello(_Payload):
    """First message from an interactive client (iOS, web)."""

    app_version: str
    protocol_version: int
    platform: str
    os_version: str | None = None


@register("server.hello_ack", MessageKind.RES)
class HelloAck(_Payload):
    """Server's answer to a hello, establishing session parameters."""

    server_version: str
    protocol_version: int
    session_id: str
    device_kind: DeviceKind
    server_time: datetime
    heartbeat_interval_s: float = Field(gt=0)


@register("conn.ping", MessageKind.CMD)
class ConnPing(_Payload):
    """Liveness probe. Either side may send it."""


@register("conn.pong", MessageKind.RES)
class ConnPong(_Payload):
    """Answer to :class:`ConnPing`, correlated by ``corr_id``."""


@register("agent.mode.changed", MessageKind.EVT)
class ModeChanged(_Payload):
    """Agent announcing a SAFE MODE transition. See docs/VISION-POLICY.md §3."""

    mode: AgentMode
    reason: str


@register("server.error", MessageKind.ERR)
class ErrorPayload(_Payload):
    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Tool execution (M2)
#
# Commands that can act on the machine are signed by the server, and the agent
# verifies them against a key it pinned at pairing time. Results are signed back
# by the device, so the audit trail records an outcome that only that device
# could have produced.
# --------------------------------------------------------------------------


@register("agent.tool.execute", MessageKind.CMD, signature_required=True)
class ToolExecute(_Payload):
    """Run one tool. Never a shell: `tool` names a declared, typed capability."""

    call_id: str
    tool: str
    tool_version: int
    args: dict[str, Any] = Field(default_factory=dict)
    #: What the server's policy assessed. The agent re-assesses independently and
    #: refuses on any disagreement rather than trusting this value.
    risk: RiskLevel
    deadline_s: float = Field(gt=0, le=600)


@register("agent.tool.cancel", MessageKind.CMD, signature_required=True)
class ToolCancel(_Payload):
    call_id: str


class ToolFailure(_Payload):
    code: str
    message: str


@register("agent.tool.result", MessageKind.RES, signature_required=True)
class ToolResult(_Payload):
    call_id: str
    tool: str
    status: ToolStatus
    result: dict[str, Any] | None = None
    failure: ToolFailure | None = None
    #: Present when ``status`` is ``refused``.
    refusal: RefusalReason | None = None
    #: The risk the *agent* computed. Recorded even when it matches, so a
    #: divergence between the two sides is visible in the audit trail.
    risk_local: RiskLevel | None = None
    duration_ms: int = Field(ge=0)


@register("agent.mode.enter_safe", MessageKind.CMD, signature_required=True)
class EnterSafeMode(_Payload):
    """Ask the agent to enter SAFE MODE.

    One-way by design. There is no message that leaves SAFE MODE: that requires
    physical access to the machine, so a compromised backend cannot re-enable
    the capabilities it just lost.
    """

    reason: str


# --------------------------------------------------------------------------
# Telemetry and activity (M2)
# --------------------------------------------------------------------------


class DiskUsage(_Payload):
    mount: str
    total_gb: float
    free_gb: float
    used_pct: float


@register("agent.telemetry", MessageKind.EVT)
class SystemTelemetry(_Payload):
    cpu_pct: float
    ram_used_pct: float
    ram_total_mb: int
    disks: tuple[DiskUsage, ...] = ()
    uptime_s: int
    #: Absent when no supported sensor is available; see PHASE-0 §2.
    gpu_temp_c: float | None = None


class ActivitySample(_Payload):
    """One observation of what the machine is being used for.

    Metadata only: the foreground process and whether the user is idle. Window
    titles, keystrokes and clipboard contents are deliberately not collected —
    see docs/security.md.
    """

    ts: datetime
    process_name: str
    is_idle: bool
    idle_seconds: int = Field(ge=0)


@register("agent.activity.batch", MessageKind.EVT)
class ActivityBatch(_Payload):
    samples: tuple[ActivitySample, ...]


# --------------------------------------------------------------------------
# Parsing and construction
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    envelope: Envelope
    payload: _Payload
    spec: MessageSpec


def parse_message(raw: str | bytes) -> ParsedMessage:
    """Parse and fully validate an inbound frame.

    Raises:
        AtlasProtocolError: with a code the caller can return verbatim to the
            peer. Every failure mode here is a protocol error, never a crash.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AtlasProtocolError(ErrorCode.MALFORMED, f"invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise AtlasProtocolError(ErrorCode.MALFORMED, "envelope must be a JSON object")

    # Version is checked before anything else: on a mismatch, no other field is
    # trustworthy and the connection is going to be closed regardless.
    version = data.get("v")
    if version is None:
        raise AtlasProtocolError(ErrorCode.MALFORMED, "missing protocol version 'v'")
    if version != PROTOCOL_VERSION:
        raise AtlasProtocolError(
            ErrorCode.UNSUPPORTED_VERSION,
            f"protocol version {version!r} is not supported",
            {"supported": PROTOCOL_VERSION, "received": version},
        )

    try:
        envelope = Envelope.model_validate(data)
    except ValidationError as exc:
        raise AtlasProtocolError(
            ErrorCode.MALFORMED,
            "envelope failed validation",
            {"errors": exc.errors(include_url=False, include_input=False)},
        ) from exc

    spec = spec_for(envelope.type)

    if envelope.kind not in spec.kinds:
        raise AtlasProtocolError(
            ErrorCode.INVALID_KIND,
            f"{envelope.type} cannot be sent as kind={envelope.kind}",
            {"allowed": sorted(k.value for k in spec.kinds)},
        )

    try:
        payload = spec.payload_model.model_validate(envelope.payload)
    except ValidationError as exc:
        raise AtlasProtocolError(
            ErrorCode.MALFORMED,
            f"payload failed validation for {envelope.type}",
            {"errors": exc.errors(include_url=False, include_input=False)},
        ) from exc

    return ParsedMessage(envelope=envelope, payload=payload, spec=spec)


def require_signature(parsed: ParsedMessage, public_key: bytes) -> None:
    """Enforce the signature a message type declares it needs.

    Called by the receiver of any message that can cause an effect. Types that
    do not declare ``signature_required`` pass through untouched, so this can be
    applied uniformly to every inbound message.

    Raises:
        AtlasProtocolError: ``SIGNATURE_INVALID`` when a required signature is
            missing or does not verify against ``public_key``.
    """
    if not parsed.spec.signature_required:
        return
    if not verify_envelope(parsed.envelope, public_key):
        raise AtlasProtocolError(
            ErrorCode.SIGNATURE_INVALID,
            f"{parsed.envelope.type} requires a valid signature",
            {"type": parsed.envelope.type},
        )


def build_envelope(
    type_name: str,
    payload: _Payload,
    *,
    kind: MessageKind | None = None,
    corr_id: str | None = None,
) -> Envelope:
    """Construct a valid envelope for ``payload``.

    Args:
        kind: Required only when the type permits more than one kind.
    """
    spec = spec_for(type_name)
    if not isinstance(payload, spec.payload_model):
        raise TypeError(
            f"{type_name} expects {spec.payload_model.__name__}, got {type(payload).__name__}"
        )

    if kind is None:
        if len(spec.kinds) != 1:
            raise ValueError(f"{type_name} allows {sorted(spec.kinds)}; kind must be explicit")
        kind = next(iter(spec.kinds))
    elif kind not in spec.kinds:
        raise ValueError(f"{type_name} cannot be sent as kind={kind}")

    return Envelope(
        id=new_ulid(),
        corr_id=corr_id,
        kind=kind,
        type=type_name,
        payload=payload.model_dump(mode="json"),
    )
