from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from atlas_shared.crypto import generate_keypair
from atlas_shared.enums import MessageKind
from atlas_shared.ids import new_ulid
from atlas_shared.protocol.envelope import (
    PROTOCOL_VERSION,
    Envelope,
    format_timestamp,
    sign_envelope,
    signing_input,
    verify_envelope,
)


def make_envelope(**overrides: object) -> Envelope:
    defaults: dict[str, object] = {
        "kind": MessageKind.CMD,
        "type": "agent.tool.execute",
        "payload": {"tool": "app.launch", "args": {"name": "chrome"}},
        "ts": datetime(2026, 8, 12, 10, 31, 2, 123_000, tzinfo=UTC),
        "id": new_ulid(timestamp_ms=1_786_000_000_000),
    }
    return Envelope(**(defaults | overrides))  # type: ignore[arg-type]


class TestFormatting:
    def test_timestamp_is_utc_millisecond_z(self) -> None:
        value = datetime(2026, 8, 12, 10, 31, 2, 123_456, tzinfo=UTC)
        assert format_timestamp(value) == "2026-08-12T10:31:02.123Z"

    def test_non_utc_input_is_converted(self) -> None:
        plus_five = timezone(timedelta(hours=5))
        value = datetime(2026, 8, 12, 15, 31, 2, 0, tzinfo=plus_five)
        assert format_timestamp(value) == "2026-08-12T10:31:02.000Z"

    def test_naive_timestamp_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            format_timestamp(datetime(2026, 8, 12, 10, 31, 2))


class TestValidation:
    def test_naive_ts_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_envelope(ts=datetime(2026, 8, 12, 10, 31, 2))

    @pytest.mark.parametrize(
        "bad_type",
        ["", "NoDots", "Upper.Case", "trailing.", ".leading", "has space.x", "1starts.digit"],
    )
    def test_malformed_type_rejected(self, bad_type: str) -> None:
        with pytest.raises(ValidationError):
            make_envelope(type=bad_type)

    def test_non_ulid_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_envelope(id="0" * 26 + "X")

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Envelope(  # type: ignore[call-arg]
                kind=MessageKind.CMD, type="conn.ping", smuggled="value"
            )

    def test_defaults_are_sane(self) -> None:
        envelope = Envelope(kind=MessageKind.CMD, type="conn.ping")
        assert envelope.v == PROTOCOL_VERSION
        assert envelope.ts.tzinfo is not None
        assert envelope.payload == {}
        assert envelope.sig is None


class TestSigning:
    def test_round_trip(self) -> None:
        private, public = generate_keypair()
        signed = sign_envelope(make_envelope(), private)
        assert signed.sig is not None
        assert verify_envelope(signed, public)

    def test_unsigned_envelope_never_verifies(self) -> None:
        _, public = generate_keypair()
        assert not verify_envelope(make_envelope(), public)

    def test_wrong_key_fails(self) -> None:
        private, _ = generate_keypair()
        _, other_public = generate_keypair()
        assert not verify_envelope(sign_envelope(make_envelope(), private), other_public)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("payload", {"tool": "fs.delete", "args": {"paths": ["C:/"]}}),
            ("type", "agent.tool.cancel"),
            ("kind", MessageKind.EVT),
            ("id", new_ulid()),
            ("corr_id", new_ulid()),
            ("ts", datetime(2026, 8, 12, 10, 31, 3, tzinfo=UTC)),
        ],
    )
    def test_tampering_with_any_signed_field_invalidates(self, field: str, value: object) -> None:
        private, public = generate_keypair()
        signed = sign_envelope(make_envelope(), private)
        tampered = signed.model_copy(update={field: value})
        assert not verify_envelope(tampered, public)

    def test_payload_key_reordering_does_not_break_signature(self) -> None:
        # Canonical encoding means a peer may re-serialise the payload with a
        # different key order and the signature must still verify.
        private, public = generate_keypair()
        signed = sign_envelope(make_envelope(payload={"a": 1, "b": 2}), private)
        reordered = signed.model_copy(update={"payload": {"b": 2, "a": 1}})
        assert verify_envelope(reordered, public)

    def test_signature_is_domain_separated(self) -> None:
        assert signing_input(make_envelope()).startswith(b"atlas.envelope.v1")

    def test_signing_input_is_unambiguous_across_field_boundaries(self) -> None:
        # Two envelopes whose fields concatenate to the same string must still
        # produce different pre-images, or a signature could be transplanted.
        left = make_envelope(type="a.bc", corr_id=None)
        right = make_envelope(type="ab.c", corr_id=None)
        assert signing_input(left) != signing_input(right)

    def test_to_json_uses_canonical_timestamp_and_omits_empty_fields(self) -> None:
        payload = make_envelope().to_json()
        assert '"ts":"2026-08-12T10:31:02.123Z"' in payload
        assert "corr_id" not in payload
        assert "sig" not in payload
