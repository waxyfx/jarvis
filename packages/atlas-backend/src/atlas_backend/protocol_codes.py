"""WebSocket close codes.

The 4000-4999 range is reserved for applications. Clients branch on these, so
they are part of the contract: see docs/protocol.md.
"""

from __future__ import annotations

from enum import IntEnum

__all__ = ["CloseCode"]


class CloseCode(IntEnum):
    #: Normal shutdown initiated by the server.
    GOING_AWAY = 1001
    #: Envelope could not be parsed or violated the schema.
    MALFORMED = 4400
    #: Token missing, invalid or expired — obtain a new one and retry.
    UNAUTHORIZED = 4401
    #: Protocol major version mismatch. Retrying will not help; upgrade.
    UNSUPPORTED_VERSION = 4402
    #: Device revoked. Re-pairing is required.
    REVOKED = 4403
    #: Hello not received in time, or heartbeats stopped.
    TIMEOUT = 4408
    #: Superseded by a newer connection from the same device.
    REPLACED = 4409
    #: A message id was reused, or a timestamp fell outside the accepted window.
    REPLAY = 4410
