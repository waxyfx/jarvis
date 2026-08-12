import json

import pytest

from atlas_shared.enums import AgentMode, MessageKind
from atlas_shared.ids import new_ulid
from atlas_shared.protocol.envelope import PROTOCOL_VERSION, Envelope
from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode
from atlas_shared.protocol.messages import (
    AgentHello,
    ConnPing,
    build_envelope,
    known_types,
    parse_message,
    spec_for,
)


def hello_frame(**overrides: object) -> str:
    envelope = build_envelope(
        "agent.hello",
        AgentHello(
            agent_version="0.1.0",
            protocol_version=PROTOCOL_VERSION,
            platform="Windows-11",
            hostname="workstation",
            mode=AgentMode.NORMAL,
            capabilities=("system", "apps"),
        ),
    )
    data = json.loads(envelope.to_json())
    data.update(overrides)
    return json.dumps(data)


class TestParsing:
    def test_valid_frame_round_trips(self) -> None:
        parsed = parse_message(hello_frame())
        assert parsed.envelope.type == "agent.hello"
        assert isinstance(parsed.payload, AgentHello)
        assert parsed.payload.hostname == "workstation"
        assert parsed.spec.kinds == {MessageKind.CMD}

    def test_bytes_input_is_accepted(self) -> None:
        assert parse_message(hello_frame().encode()).envelope.type == "agent.hello"

    @pytest.mark.parametrize("raw", ["", "not json", "{", '{"v":1', "[]", '"a string"', "42"])
    def test_malformed_input(self, raw: str) -> None:
        with pytest.raises(AtlasProtocolError) as exc:
            parse_message(raw)
        assert exc.value.code is ErrorCode.MALFORMED

    @pytest.mark.parametrize("mutate", ["delete", "null"])
    def test_absent_version_is_malformed_not_a_version_mismatch(self, mutate: str) -> None:
        # A missing or null 'v' is a broken frame, not a peer speaking another
        # protocol version — the two deserve different codes.
        data = json.loads(hello_frame())
        if mutate == "delete":
            del data["v"]
        else:
            data["v"] = None
        with pytest.raises(AtlasProtocolError) as exc:
            parse_message(json.dumps(data))
        assert exc.value.code is ErrorCode.MALFORMED

    @pytest.mark.parametrize("version", [0, 2, 99, "1"])
    def test_version_mismatch_is_reported_before_anything_else(self, version: object) -> None:
        # Even with an otherwise broken frame, version must win: on a mismatch
        # no other field is trustworthy.
        with pytest.raises(AtlasProtocolError) as exc:
            parse_message(hello_frame(v=version, type="does.not.exist"))
        assert exc.value.code is ErrorCode.UNSUPPORTED_VERSION

    def test_unknown_type(self) -> None:
        with pytest.raises(AtlasProtocolError) as exc:
            parse_message(hello_frame(type="agent.definitely_not_real"))
        assert exc.value.code is ErrorCode.UNSUPPORTED_TYPE

    def test_wrong_kind_for_type(self) -> None:
        with pytest.raises(AtlasProtocolError) as exc:
            parse_message(hello_frame(kind="evt"))
        assert exc.value.code is ErrorCode.INVALID_KIND

    def test_payload_missing_required_field(self) -> None:
        with pytest.raises(AtlasProtocolError) as exc:
            parse_message(hello_frame(payload={"agent_version": "0.1.0"}))
        assert exc.value.code is ErrorCode.MALFORMED

    def test_payload_with_unknown_field_is_rejected(self) -> None:
        data = json.loads(hello_frame())
        data["payload"]["smuggled"] = "value"
        with pytest.raises(AtlasProtocolError) as exc:
            parse_message(json.dumps(data))
        assert exc.value.code is ErrorCode.MALFORMED

    def test_envelope_with_unknown_field_is_rejected(self) -> None:
        with pytest.raises(AtlasProtocolError) as exc:
            parse_message(hello_frame(smuggled="value"))
        assert exc.value.code is ErrorCode.MALFORMED


class TestConstruction:
    def test_kind_is_inferred_when_unambiguous(self) -> None:
        envelope = build_envelope("conn.ping", ConnPing())
        assert envelope.kind is MessageKind.CMD

    def test_payload_model_must_match_type(self) -> None:
        with pytest.raises(TypeError, match="expects"):
            build_envelope("agent.hello", ConnPing())

    def test_explicit_kind_must_be_permitted(self) -> None:
        with pytest.raises(ValueError, match="cannot be sent as"):
            build_envelope("conn.ping", ConnPing(), kind=MessageKind.ERR)

    def test_correlation_id_is_carried(self) -> None:
        request_id = new_ulid()
        envelope = build_envelope("conn.ping", ConnPing(), corr_id=request_id)
        assert envelope.corr_id == request_id

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(AtlasProtocolError) as exc:
            build_envelope("nope.nope", ConnPing())
        assert exc.value.code is ErrorCode.UNSUPPORTED_TYPE


class TestRegistry:
    def test_every_registered_type_is_a_valid_envelope_type(self) -> None:
        for type_name in known_types():
            Envelope(kind=next(iter(spec_for(type_name).kinds)), type=type_name)

    def test_m1_lifecycle_types_are_present(self) -> None:
        assert {
            "agent.hello",
            "client.hello",
            "server.hello_ack",
            "conn.ping",
            "conn.pong",
            "agent.mode.changed",
            "server.error",
        } <= known_types()

    def test_no_type_declares_zero_kinds(self) -> None:
        assert all(spec_for(name).kinds for name in known_types())
