"""The server's own signing identity.

Devices pin this key at pairing time and refuse commands signed by anything
else. That closes a gap the M1 design left open: a bearer token proves *the
backend* is talking, but not *which* backend — an attacker who obtained a token
could otherwise direct the agent from their own server.

The key lives in the environment, not the database, for the same reason the
agent's key lives in DPAPI: a dump of the database must not yield the ability to
sign commands.

Rotation is deliberately disruptive. Changing this key invalidates every device's
pin, and each device must be paired again. That is the correct trade: a key that
can be swapped quietly is a key an attacker can swap quietly.
"""

from __future__ import annotations

from atlas_backend.config import Settings
from atlas_shared.crypto import KEY_SIZE, b64u_decode, b64u_encode, public_key_for
from atlas_shared.protocol.envelope import Envelope, sign_envelope

__all__ = ["ServerIdentity"]


class ServerIdentity:
    def __init__(self, settings: Settings) -> None:
        private_key = b64u_decode(settings.server_signing_key.get_secret_value())
        if len(private_key) != KEY_SIZE:
            raise ValueError(
                f"server_signing_key must decode to {KEY_SIZE} bytes, got {len(private_key)}"
            )
        self._private_key = private_key
        self._public_key = public_key_for(private_key)

    @property
    def public_key(self) -> bytes:
        return self._public_key

    @property
    def public_key_b64(self) -> str:
        """The form devices store and compare against."""
        return b64u_encode(self._public_key)

    def sign(self, envelope: Envelope) -> Envelope:
        """Return a copy of ``envelope`` signed with the server key."""
        return sign_envelope(envelope, self._private_key)
