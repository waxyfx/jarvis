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

from atlas_shared.enums import AgentMode, DeviceKind, MessageKind
from atlas_shared.ids import new_ulid
from atlas_shared.protocol.envelope import PROTOCOL_VERSION, Envelope
from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode

__all__ = [
    "AgentHello",
    "ClientHello",
    "ConnPing",
    "ConnPong",
    "ErrorPayload",
    "HelloAck",
    "MessageSpec",
    "ModeChanged",
    "ParsedMessage",
    "build_envelope",
    "known_types",
    "parse_message",
    "register",
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
