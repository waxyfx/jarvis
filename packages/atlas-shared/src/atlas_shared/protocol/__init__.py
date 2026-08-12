"""ATLAS wire protocol."""

from atlas_shared.protocol.envelope import (
    PROTOCOL_VERSION,
    Envelope,
    format_timestamp,
    sign_envelope,
    signing_input,
    verify_envelope,
)
from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode
from atlas_shared.protocol.messages import (
    AgentHello,
    ClientHello,
    ConnPing,
    ConnPong,
    ErrorPayload,
    HelloAck,
    MessageSpec,
    ModeChanged,
    ParsedMessage,
    build_envelope,
    known_types,
    parse_message,
    register,
    spec_for,
)

__all__ = [
    "PROTOCOL_VERSION",
    "AgentHello",
    "AtlasProtocolError",
    "ClientHello",
    "ConnPing",
    "ConnPong",
    "Envelope",
    "ErrorCode",
    "ErrorPayload",
    "HelloAck",
    "MessageSpec",
    "ModeChanged",
    "ParsedMessage",
    "build_envelope",
    "format_timestamp",
    "known_types",
    "parse_message",
    "register",
    "sign_envelope",
    "signing_input",
    "spec_for",
    "verify_envelope",
]
