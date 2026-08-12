"""Registry of live device connections.

One connection per device: registering a second one closes the first. Without
that rule there would be no single answer to "where do I send this command?",
and a stale socket could silently swallow instructions meant for the live one.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import WebSocket

from atlas_backend.db.base import utc_now
from atlas_backend.protocol_codes import CloseCode

__all__ = ["Connection", "Hub"]


@dataclass(slots=True)
class Connection:
    device_id: uuid.UUID
    device_kind: str
    session_id: uuid.UUID
    websocket: WebSocket
    connected_at: datetime = field(default_factory=utc_now)
    last_seen_at: datetime = field(default_factory=utc_now)
    handshake_complete: bool = False

    def touch(self) -> None:
        self.last_seen_at = utc_now()


class Hub:
    def __init__(self) -> None:
        self._connections: dict[uuid.UUID, Connection] = {}
        self._lock = asyncio.Lock()

    async def register(self, connection: Connection) -> None:
        """Add a connection, evicting any existing one for the same device."""
        async with self._lock:
            existing = self._connections.get(connection.device_id)
            self._connections[connection.device_id] = connection

        if existing is not None:
            await _close_quietly(existing.websocket, CloseCode.REPLACED)

    async def unregister(self, connection: Connection) -> None:
        async with self._lock:
            current = self._connections.get(connection.device_id)
            # Only drop the entry if it is still *this* connection: a newer one
            # may have replaced it while this coroutine was unwinding.
            if current is not None and current.session_id == connection.session_id:
                del self._connections[connection.device_id]

    def get(self, device_id: uuid.UUID) -> Connection | None:
        return self._connections.get(device_id)

    def is_connected(self, device_id: uuid.UUID) -> bool:
        return device_id in self._connections

    def snapshot(self) -> tuple[Connection, ...]:
        return tuple(self._connections.values())

    async def send(self, device_id: uuid.UUID, payload: str) -> bool:
        """Send a serialised envelope. Returns False if the device is not connected."""
        connection = self._connections.get(device_id)
        if connection is None:
            return False
        try:
            await connection.websocket.send_text(payload)
        except (RuntimeError, ConnectionError):
            # The socket died between the lookup and the write.
            return False
        return True

    async def disconnect(self, device_id: uuid.UUID, *, reason: str) -> bool:
        """Force a device off. Used by revocation and emergency disconnect."""
        async with self._lock:
            connection = self._connections.pop(device_id, None)
        if connection is None:
            return False
        code = CloseCode.REVOKED if reason == "revoked" else CloseCode.GOING_AWAY
        await _close_quietly(connection.websocket, code)
        return True

    async def close_all(self, *, reason: str = "shutdown") -> int:
        async with self._lock:
            connections = tuple(self._connections.values())
            self._connections.clear()
        for connection in connections:
            await _close_quietly(connection.websocket, CloseCode.GOING_AWAY)
        return len(connections)


async def _close_quietly(websocket: WebSocket, code: int) -> None:
    """Close without letting an already-dead socket raise."""
    try:
        await websocket.close(code=code)
    except (RuntimeError, ConnectionError):
        pass
