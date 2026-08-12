"""Ed25519 primitives shared by every ATLAS component.

Device identity is an Ed25519 keypair. The private key never leaves the device
that generated it (DPAPI on Windows, Secure Enclave on iOS); the server stores
only the public half. That way a compromise of the server database cannot forge
a command to anyone's computer.

Keys are handled in raw 32-byte form on the wire and in the database — not PEM —
because the format is fixed-length, unambiguous, and has no parser to attack.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

__all__ = [
    "KEY_SIZE",
    "SIGNATURE_SIZE",
    "b64u_decode",
    "b64u_encode",
    "constant_time_equals",
    "generate_keypair",
    "public_key_for",
    "sha256_hex",
    "sign",
    "verify",
]

KEY_SIZE = 32
SIGNATURE_SIZE = 64


def generate_keypair() -> tuple[bytes, bytes]:
    """Return ``(private_key, public_key)`` as raw 32-byte values."""
    private = Ed25519PrivateKey.generate()
    return _private_bytes(private), _public_bytes(private.public_key())


def public_key_for(private_key: bytes) -> bytes:
    """Derive the public half of ``private_key``."""
    return _public_bytes(_load_private(private_key).public_key())


def sign(private_key: bytes, message: bytes) -> bytes:
    """Sign ``message``, returning a raw 64-byte signature."""
    return _load_private(private_key).sign(message)


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Whether ``signature`` is a valid Ed25519 signature over ``message``.

    Returns ``False`` for malformed keys and signatures rather than raising, so
    that callers cannot accidentally distinguish "bad input" from "bad
    signature" — both mean the same thing at the call site: reject.
    """
    if len(signature) != SIGNATURE_SIZE:
        return False
    try:
        _load_public(public_key).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def b64u_encode(data: bytes) -> str:
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    """Inverse of :func:`b64u_encode`. Raises ``ValueError`` on bad input."""
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except Exception as exc:
        raise ValueError(f"invalid base64url: {exc}") from exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def constant_time_equals(left: str | bytes, right: str | bytes) -> bool:
    """Timing-safe comparison for secrets (pairing codes, digests, tokens)."""
    left_bytes = left.encode("utf-8") if isinstance(left, str) else left
    right_bytes = right.encode("utf-8") if isinstance(right, str) else right
    return hmac.compare_digest(left_bytes, right_bytes)


def _load_private(private_key: bytes) -> Ed25519PrivateKey:
    if len(private_key) != KEY_SIZE:
        raise ValueError(f"Ed25519 private key must be {KEY_SIZE} bytes, got {len(private_key)}")
    return Ed25519PrivateKey.from_private_bytes(private_key)


def _load_public(public_key: bytes) -> Ed25519PublicKey:
    if len(public_key) != KEY_SIZE:
        raise ValueError(f"Ed25519 public key must be {KEY_SIZE} bytes, got {len(public_key)}")
    return Ed25519PublicKey.from_public_bytes(public_key)


def _private_bytes(key: Ed25519PrivateKey) -> bytes:
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    return key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())


def _public_bytes(key: Ed25519PublicKey) -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)
