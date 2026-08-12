import pytest

from atlas_shared.crypto import (
    KEY_SIZE,
    SIGNATURE_SIZE,
    b64u_decode,
    b64u_encode,
    constant_time_equals,
    generate_keypair,
    public_key_for,
    sign,
    verify,
)


def test_keypair_shape() -> None:
    private, public = generate_keypair()
    assert len(private) == KEY_SIZE
    assert len(public) == KEY_SIZE
    assert private != public


def test_public_key_is_derivable_from_private() -> None:
    private, public = generate_keypair()
    assert public_key_for(private) == public


def test_sign_and_verify_round_trip() -> None:
    private, public = generate_keypair()
    message = b"open chrome"
    signature = sign(private, message)
    assert len(signature) == SIGNATURE_SIZE
    assert verify(public, message, signature)


def test_tampered_message_fails() -> None:
    private, public = generate_keypair()
    signature = sign(private, b"delete one file")
    assert not verify(public, b"delete all files", signature)


def test_signature_from_another_key_fails() -> None:
    private_a, _ = generate_keypair()
    _, public_b = generate_keypair()
    signature = sign(private_a, b"payload")
    assert not verify(public_b, b"payload", signature)


def test_flipped_signature_bit_fails() -> None:
    private, public = generate_keypair()
    message = b"payload"
    signature = bytearray(sign(private, message))
    signature[0] ^= 0x01
    assert not verify(public, message, bytes(signature))


@pytest.mark.parametrize("bad_signature", [b"", b"\x00" * 63, b"\x00" * 65])
def test_malformed_signature_returns_false_not_raises(bad_signature: bytes) -> None:
    _, public = generate_keypair()
    assert not verify(public, b"payload", bad_signature)


@pytest.mark.parametrize("bad_key", [b"", b"\x00" * 31, b"\x00" * 33])
def test_malformed_public_key_returns_false_not_raises(bad_key: bytes) -> None:
    private, _ = generate_keypair()
    signature = sign(private, b"payload")
    # A caller must not be able to tell "bad key" from "bad signature": both
    # mean reject, and distinguishing them leaks information.
    assert not verify(bad_key, b"payload", signature)


@pytest.mark.parametrize("bad_key", [b"", b"\x00" * 31, b"\x00" * 33])
def test_malformed_private_key_raises(bad_key: bytes) -> None:
    with pytest.raises(ValueError, match="private key must be"):
        sign(bad_key, b"payload")


@pytest.mark.parametrize("data", [b"", b"\x00", b"\xff" * 32, bytes(range(256))])
def test_b64u_round_trip(data: bytes) -> None:
    encoded = b64u_encode(data)
    assert "=" not in encoded
    assert "+" not in encoded
    assert "/" not in encoded
    assert b64u_decode(encoded) == data


def test_b64u_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="invalid base64url"):
        b64u_decode("!!!not base64!!!")


def test_constant_time_equals() -> None:
    assert constant_time_equals("abc", "abc")
    assert constant_time_equals(b"abc", "abc")
    assert not constant_time_equals("abc", "abd")
    assert not constant_time_equals("abc", "abcd")
