"""The realtime endpoint.

Lifecycle: authenticate → accept → hello/hello_ack → serve until the peer goes
away or stops answering heartbeats. Authentication happens *before* the socket
is accepted, so an unauthenticated peer never gets an open connection; the
handshake is rejected with HTTP 403 and the client knows to re-authenticate.

M1 handles the connection lifecycle only. Tool dispatch, voice and media
messages arrive in later phases and plug into :func:`_dispatch`.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket
from sqlalchemy import select

from atlas_backend import __version__
from atlas_backend.audit import AuditActor, AuditEvent, append
from atlas_backend.auth.challenge import load_active_device
from atlas_backend.config import Settings
from atlas_backend.db.base import utc_now
from atlas_backend.db.models import ActivitySampleRow, DeviceSession, SystemTelemetryRow
from atlas_backend.db.session import Database
from atlas_backend.logging import get_logger
from atlas_backend.protocol_codes import CloseCode
from atlas_backend.ws.hub import Connection
from atlas_backend.ws.replay import ReplayGuard
from atlas_shared.enums import DeviceKind, MessageKind
from atlas_shared.protocol.envelope import PROTOCOL_VERSION, Envelope
from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode
from atlas_shared.protocol.messages import (
    ActivityBatch,
    AgentHello,
    ClientHello,
    ConnPing,
    ConnPong,
    ErrorPayload,
    HelloAck,
    ModeChanged,
    ParsedMessage,
    SystemTelemetry,
    ToolResult,
    build_envelope,
    parse_message,
    require_signature,
)

router = APIRouter()
log = get_logger(__name__)

#: A device may only introduce itself as the kind it was enrolled as.
_EXPECTED_HELLO = {
    DeviceKind.WINDOWS_AGENT.value: "agent.hello",
    DeviceKind.IOS.value: "client.hello",
    DeviceKind.WEB.value: "client.hello",
}


# These two carry control flow, not failure, so the "...Error" naming
# convention would misdescribe them.
class _Disconnected(Exception):  # noqa: N818
    """The peer closed the socket."""


class _CloseConnection(Exception):  # noqa: N818
    """We are closing the socket; carries the code and the audit reason."""

    def __init__(self, code: CloseCode, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@router.websocket("/v1/ws")
async def device_socket(websocket: WebSocket) -> None:
    app = websocket.app
    settings = app.state.settings
    database = app.state.database
    hub = app.state.hub

    identity = await _authenticate(websocket)
    if identity is None:
        # Rejected during the handshake: no socket is ever accepted.
        await websocket.close(code=CloseCode.UNAUTHORIZED)
        return
    device_id, device_kind, device_public_key = identity

    await websocket.accept()

    session_id = uuid.uuid4()
    async with database.transaction() as db:
        db.add(DeviceSession(id=session_id, device_id=device_id, remote_addr=_peer(websocket)))
        await append(
            db,
            actor=AuditActor.DEVICE,
            event_type=AuditEvent.CONNECTION_OPENED,
            device_id=device_id,
            payload={"session_id": str(session_id), "remote_addr": _peer(websocket)},
        )

    connection = Connection(
        device_id=device_id,
        device_kind=device_kind,
        session_id=session_id,
        websocket=websocket,
        public_key=device_public_key,
    )
    await hub.register(connection)

    guard = ReplayGuard(skew_tolerance_s=settings.clock_skew_tolerance_s)
    close_code = CloseCode.GOING_AWAY
    close_reason = "peer disconnected"

    try:
        await _handshake(websocket, connection, guard, settings)
        async with database.transaction() as db:
            row = await db.get(DeviceSession, session_id)
            if row is not None:
                row.handshake_ok = True
        await _serve(websocket, connection, guard, settings, database)
    except _Disconnected:
        pass
    except _CloseConnection as exc:
        close_code, close_reason = exc.code, exc.reason
        await _close(websocket, exc.code)
    except Exception:
        close_code, close_reason = CloseCode.GOING_AWAY, "internal error"
        log.exception("websocket_handler_failed", device_id=str(device_id))
        await _close(websocket, CloseCode.GOING_AWAY)
        raise
    finally:
        await hub.unregister(connection)
        async with database.transaction() as db:
            row = (
                await db.execute(select(DeviceSession).where(DeviceSession.id == session_id))
            ).scalar_one_or_none()
            if row is not None:
                row.ended_at = utc_now()
                row.close_reason = close_reason[:120]
            await append(
                db,
                actor=AuditActor.DEVICE,
                event_type=AuditEvent.CONNECTION_CLOSED,
                device_id=device_id,
                payload={
                    "session_id": str(session_id),
                    "close_code": int(close_code),
                    "reason": close_reason,
                },
            )


# ---------------------------------------------------------------- lifecycle


async def _authenticate(websocket: WebSocket) -> tuple[uuid.UUID, str, bytes] | None:
    """Validate the bearer token and confirm the device is still active."""
    tokens = websocket.app.state.token_service
    database = websocket.app.state.database

    header = websocket.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None

    try:
        claims = tokens.verify(token.strip())
        async with database.transaction() as db:
            device = await load_active_device(db, claims.device_id)
            device.last_seen_at = utc_now()
            return device.id, device.kind, device.public_key
    except AtlasProtocolError:
        return None


async def _handshake(
    websocket: WebSocket,
    connection: Connection,
    guard: ReplayGuard,
    settings: Settings,
) -> None:
    """Require a well-formed hello before anything else is accepted."""
    try:
        raw = await asyncio.wait_for(_receive_text(websocket), timeout=settings.hello_timeout_s)
    except TimeoutError as exc:
        raise _CloseConnection(CloseCode.TIMEOUT, "hello not received in time") from exc

    parsed = _parse_or_close(raw, guard)

    expected = _EXPECTED_HELLO.get(connection.device_kind)
    if parsed.envelope.type != expected:
        raise _CloseConnection(
            CloseCode.MALFORMED,
            f"expected {expected} as the first message, got {parsed.envelope.type}",
        )

    payload = parsed.payload
    assert isinstance(payload, AgentHello | ClientHello)
    if payload.protocol_version != PROTOCOL_VERSION:
        raise _CloseConnection(
            CloseCode.UNSUPPORTED_VERSION,
            f"peer speaks protocol {payload.protocol_version}",
        )

    connection.handshake_complete = True
    if isinstance(payload, AgentHello):
        connection.mode = payload.mode
    connection.touch()

    await _send(
        websocket,
        build_envelope(
            "server.hello_ack",
            HelloAck(
                server_version=__version__,
                protocol_version=PROTOCOL_VERSION,
                session_id=str(connection.session_id),
                device_kind=DeviceKind(connection.device_kind),
                server_time=utc_now(),
                heartbeat_interval_s=settings.heartbeat_interval_s,
            ),
            corr_id=parsed.envelope.id,
        ),
    )


async def _serve(
    websocket: WebSocket,
    connection: Connection,
    guard: ReplayGuard,
    settings: Settings,
    database: Database,
) -> None:
    """Receive loop with an inline heartbeat.

    One loop rather than two tasks: the receive timeout *is* the heartbeat tick,
    which removes any need to synchronise a separate pinger with the socket.
    """
    interval = settings.heartbeat_interval_s
    deadline = interval * settings.heartbeat_grace_periods

    while True:
        try:
            raw = await asyncio.wait_for(_receive_text(websocket), timeout=interval)
        except TimeoutError:
            silent_for = (utc_now() - connection.last_seen_at).total_seconds()
            if silent_for > deadline:
                raise _CloseConnection(
                    CloseCode.TIMEOUT, f"no traffic for {silent_for:.0f}s"
                ) from None
            await _send(websocket, build_envelope("conn.ping", ConnPing()))
            continue

        connection.touch()
        try:
            parsed = _parse_or_close(raw, guard)
        except AtlasProtocolError as exc:
            # Recoverable: tell the peer and keep the connection.
            await _send_error(websocket, exc)
            continue

        await _dispatch(websocket, connection, parsed, database)


async def _dispatch(
    websocket: WebSocket,
    connection: Connection,
    parsed: ParsedMessage,
    database: Database,
) -> None:
    message_type = parsed.envelope.type

    if message_type == "conn.ping":
        await _send(
            websocket,
            build_envelope("conn.pong", ConnPong(), corr_id=parsed.envelope.id),
        )
        return

    if message_type == "conn.pong":
        return  # liveness already recorded by touch()

    if message_type == "agent.mode.changed":
        payload = parsed.payload
        assert isinstance(payload, ModeChanged)
        connection.mode = payload.mode
        async with database.transaction() as db:
            await append(
                db,
                actor=AuditActor.DEVICE,
                event_type=AuditEvent.AGENT_MODE_CHANGED,
                device_id=connection.device_id,
                payload={"mode": payload.mode.value, "reason": payload.reason},
            )
        return

    if message_type == "agent.tool.result":
        await _handle_tool_result(websocket, connection, parsed, database)
        return

    if message_type == "agent.telemetry":
        payload = parsed.payload
        assert isinstance(payload, SystemTelemetry)
        async with database.transaction() as db:
            db.add(
                SystemTelemetryRow(
                    device_id=connection.device_id,
                    ts=utc_now(),
                    cpu_pct=payload.cpu_pct,
                    ram_used_pct=payload.ram_used_pct,
                    ram_total_mb=payload.ram_total_mb,
                    disks=[disk.model_dump(mode="json") for disk in payload.disks],
                    uptime_s=payload.uptime_s,
                    gpu_temp_c=payload.gpu_temp_c,
                )
            )
        return

    if message_type == "agent.activity.batch":
        payload = parsed.payload
        assert isinstance(payload, ActivityBatch)
        async with database.transaction() as db:
            for sample in payload.samples:
                db.add(
                    ActivitySampleRow(
                        device_id=connection.device_id,
                        ts=sample.ts,
                        process_name=sample.process_name,
                        is_idle=sample.is_idle,
                        idle_seconds=sample.idle_seconds,
                    )
                )
        return

    if message_type in _EXPECTED_HELLO.values():
        raise _CloseConnection(CloseCode.MALFORMED, "duplicate hello")

    # Known to the protocol but not handled in this phase. Answering instead of
    # closing keeps a newer client usable against an older server.
    await _send_error(
        websocket,
        AtlasProtocolError(
            ErrorCode.UNSUPPORTED_TYPE,
            f"{message_type} is not handled by this server yet",
            {"type": message_type},
        ),
        corr_id=parsed.envelope.id,
    )


async def _handle_tool_result(
    websocket: WebSocket,
    connection: Connection,
    parsed: ParsedMessage,
    database: Database,
) -> None:
    """Accept a result only if the device that ran it signed it.

    An unsigned or wrongly signed result is dropped, not applied: the audit
    trail must record what the *device* reported, not what the transport
    happened to carry.
    """
    try:
        require_signature(parsed, connection.public_key)
    except AtlasProtocolError as exc:
        log.error(
            "tool_result_signature_invalid",
            device_id=str(connection.device_id),
            error=exc.message,
        )
        async with database.transaction() as db:
            await append(
                db,
                actor=AuditActor.DEVICE,
                event_type=AuditEvent.TOOL_RESULT_UNVERIFIED,
                device_id=connection.device_id,
                payload={"corr_id": parsed.envelope.corr_id},
            )
        await _send_error(websocket, exc, corr_id=parsed.envelope.id)
        return

    payload = parsed.payload
    assert isinstance(payload, ToolResult)
    if not websocket.app.state.hub.resolve(parsed.envelope.corr_id, payload):
        # Late or unsolicited: the dispatcher already gave up, or nobody asked.
        log.warning("tool_result_unmatched", call_id=payload.call_id)


# ---------------------------------------------------------------- plumbing


def _parse_or_close(raw: str, guard: ReplayGuard) -> ParsedMessage:
    """Parse a frame, converting unrecoverable faults into a close."""
    try:
        parsed = parse_message(raw)
    except AtlasProtocolError as exc:
        if exc.code is ErrorCode.UNSUPPORTED_VERSION:
            raise _CloseConnection(CloseCode.UNSUPPORTED_VERSION, exc.message) from exc
        if exc.code is ErrorCode.MALFORMED:
            raise _CloseConnection(CloseCode.MALFORMED, exc.message) from exc
        raise

    try:
        guard.check(parsed.envelope, now=datetime.now(UTC))
    except AtlasProtocolError as exc:
        raise _CloseConnection(CloseCode.REPLAY, exc.message) from exc

    return parsed


async def _receive_text(websocket: WebSocket) -> str:
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise _Disconnected

    text = message.get("text")
    if text is not None:
        return str(text)

    data = message.get("bytes")
    if data is None:
        raise _CloseConnection(CloseCode.MALFORMED, "empty frame")
    try:
        return bytes(data).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _CloseConnection(CloseCode.MALFORMED, "frame is not valid UTF-8") from exc


async def _send(websocket: WebSocket, envelope: Envelope) -> None:
    await websocket.send_text(envelope.to_json())


async def _send_error(
    websocket: WebSocket, error: AtlasProtocolError, *, corr_id: str | None = None
) -> None:
    await _send(
        websocket,
        build_envelope(
            "server.error",
            ErrorPayload(code=error.code, message=error.message, details=error.details),
            kind=MessageKind.ERR,
            corr_id=corr_id,
        ),
    )


async def _close(websocket: WebSocket, code: CloseCode) -> None:
    try:
        await websocket.close(code=code)
    except (RuntimeError, ConnectionError):
        pass


def _peer(websocket: WebSocket) -> str | None:
    client = websocket.client
    return client.host if client else None
