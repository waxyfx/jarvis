"""Signing inputs for device enrolment and authentication.

Both the backend and every device compute these, so they live here rather than
being written twice. A mismatch would show up only as an unexplained signature
failure at pairing time, which is exactly the kind of bug a shared module
prevents.

Each context has its own domain string, so a signature produced for one purpose
can never be replayed as a signature for another.
"""

from __future__ import annotations

import hashlib

__all__ = [
    "PAIRING_CODE_ALPHABET",
    "PAIRING_CODE_LENGTH",
    "challenge_signing_input",
    "normalise_pairing_code",
    "pairing_code_hash",
    "pairing_signing_input",
]

_SEPARATOR = b"\x1f"
_CHALLENGE_DOMAIN = b"atlas.auth.challenge.v1"
_PAIRING_DOMAIN = b"atlas.pair.proof.v1"

#: Crockford base32 minus I, L, O and U — no character pairs a human can
#: confuse when reading a code aloud or typing it on a phone.
PAIRING_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PAIRING_CODE_LENGTH = 8


def challenge_signing_input(device_id: str, nonce: bytes) -> bytes:
    """Bytes a device signs to prove possession of its private key."""
    return _SEPARATOR.join((_CHALLENGE_DOMAIN, device_id.encode("ascii"), nonce))


def pairing_signing_input(code: str, public_key: bytes) -> bytes:
    """Bytes a device signs when enrolling.

    Binds the pairing code to the public key being enrolled, so an intercepted
    code cannot be used to register a different key.
    """
    normalised = normalise_pairing_code(code).encode("ascii")
    return _SEPARATOR.join((_PAIRING_DOMAIN, normalised, public_key))


def normalise_pairing_code(code: str) -> str:
    """Accept what a human typed; produce the one canonical form.

    Separators and case are stripped, so ``4f2k-9x1m``, ``4F2K 9X1M`` and
    ``4F2K9X1M`` are the same code.
    """
    cleaned = "".join(character for character in code.upper() if character.isalnum())
    if len(cleaned) != PAIRING_CODE_LENGTH:
        raise ValueError(f"pairing code must have {PAIRING_CODE_LENGTH} characters")
    if any(character not in PAIRING_CODE_ALPHABET for character in cleaned):
        raise ValueError("pairing code contains characters outside the code alphabet")
    return cleaned


def pairing_code_hash(code: str) -> bytes:
    """Lookup key for a pairing code. The code itself is never stored."""
    return hashlib.sha256(normalise_pairing_code(code).encode("ascii")).digest()
