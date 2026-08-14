"""Outbound realtime transport.

The agent always dials out: it never listens on a port, so the Windows machine
is never exposed to the network. Connection state is a small loop —
authenticate, connect, handshake, serve — wrapped in reconnection with
exponential backoff.

Close codes are treated as instructions. Some mean "try again", one means
"get a new token", and two mean "stop and tell the operator": retrying a
version mismatch or a revoked device forever would just hide the problem.
"""

from __future__ import annotations

import asyncio
import contextlib
import platform
import random
import ssl
from collections.abc import Awaitable, Callable

import websockets
from websockets.asyncio.client import ClientConnection, connect

from atlas_agent import __version__
from atlas_agent.backend import BackendClient, BackendError
from atlas_agent.config import AgentSettings
from atlas_agent.identity import DeviceIdentity
from atlas_agent.logging import get_logger
from atlas_agent.monitor import ActivityMonitor
from atlas_agent.runner import ToolRunner
from atlas_agent.safety.mode import ModeChangeSource, SafeModeController
from atlas_shared.enums import AgentMode, RefusalReason, ToolStatus
from atlas_shared.ids import new_ulid
from atlas_shared.protocol.envelope import PROTOCOL_VERSION, sign_envelope
from atlas_shared.protocol.errors import AtlasProtocolError
from atlas_shared.protocol.messages import (
    ActivityBatch,
    AgentHello,
    ConnPong,
    EnterSafeMode,
    ErrorPayload,
    HelloAck,
    ModeChanged,
    ParsedMessage,
    SystemTelemetry,
    ToolExecute,
    ToolResult,
    build_envelope,
    parse_message,
    require_signature,
)
from atlas_shared.replay import ReplayGuard

__all__ = ["AgentTransport", "FatalTransportError"]

log = get_logger(__name__)

MessageHandler = Callable[[ParsedMessage, ClientConnection], Awaitable[None]]

#: Mirrors atlas_backend.protocol_codes.CloseCode; duplicated rather than
#: imported because the agent must not depend on the backend package.
_CLOSE_MALFORMED = 4400
_CLOSE_UNAUTHORIZED = 4401
_CLOSE_UNSUPPORTED_VERSION = 4402
_CLOSE_REVOKED = 4403
_CLOSE_TIMEOUT = 4408
_CLOSE_REPLACED = 4409
_CLOSE_REPLAY = 4410

_FATAL_CLOSE_CODES = {_CLOSE_UNSUPPORTED_VERSION, _CLOSE_REVOKED}


class FatalTransportError(RuntimeError):
    """Reconnecting cannot fix this; the operator has to act."""


class AgentTransport:
    def __init__(
        self,
        settings: AgentSettings,
        identity: DeviceIdentity,
        *,
        runner: ToolRunner | None = None,
        safe_mode: SafeModeController | None = None,
        monitor: ActivityMonitor | None = None,
        capabilities: tuple[str, ...] = (),
        on_message: MessageHandler | None = None,
    ) -> None:
        self._settings = settings
        self._identity = identity
        self._runner = runner
        self._safe_mode = safe_mode
        self._monitor = monitor
        self._capabilities = capabilities
        self._on_message = on_message
        self._backend = BackendClient(settings)
        self._connected = asyncio.Event()
        # Deliberately owned by the transport, not by a connection: a command
        # replayed after a reconnect must be caught too.
        self._replay = ReplayGuard(skew_tolerance_s=settings.command_freshness_s)

    @property
    def _mode(self) -> AgentMode:
        """The agent's mode, read from the controller that owns it."""
        return self._safe_mode.mode if self._safe_mode is not None else AgentMode.NORMAL

    @property
    def connected(self) -> asyncio.Event:
        """Set while a handshaked session is live. Useful for tests and the tray."""
        return self._connected

    async def run(
        self,
        *,
        stop: asyncio.Event | None = None,
        max_sessions: int | None = None,
    ) -> None:
        """Connect and stay connected until ``stop`` is set.

        Args:
            max_sessions: Stop after this many completed sessions. Only used by
                tests; production runs unbounded.
        """
        stop_event = stop or asyncio.Event()
        delay = self._settings.reconnect_initial_s
        sessions = 0

        while not stop_event.is_set():
            try:
                token = await self._backend.authenticate(self._identity)
                await self._session(token, stop_event)
                delay = self._settings.reconnect_initial_s
            except FatalTransportError:
                raise
            except (BackendError, OSError, websockets.exceptions.WebSocketException) as exc:
                delay = self._next_delay(delay, exc)
                log.warning("agent_reconnecting", error=str(exc), delay_s=round(delay, 1))
            finally:
                self._connected.clear()

            sessions += 1
            if max_sessions is not None and sessions >= max_sessions:
                return
            if stop_event.is_set():
                return

            # Sleep for the backoff, but wake immediately if asked to stop.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=delay)

    async def _session(self, token: str, stop: asyncio.Event) -> None:
        ssl_context: ssl.SSLContext | None = None
        if self._settings.websocket_url.startswith("wss://") and not self._settings.verify_tls:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        async with connect(
            self._settings.websocket_url,
            additional_headers={"Authorization": f"Bearer {token}"},
            open_timeout=self._settings.request_timeout_s,
            ssl=ssl_context,
        ) as websocket:
            await self._handshake(websocket)
            self._connected.set()
            log.info("agent_connected", url=self._settings.websocket_url)
            await self._serve(websocket, stop)

    async def _handshake(self, websocket: ClientConnection) -> None:
        hello = build_envelope(
            "agent.hello",
            AgentHello(
                agent_version=__version__,
                protocol_version=PROTOCOL_VERSION,
                platform=f"{platform.system()}-{platform.release()}",
                hostname=self._settings.device_name,
                mode=self._mode,
                capabilities=self._capabilities,
            ),
        )
        await websocket.send(hello.to_json())

        raw = await asyncio.wait_for(websocket.recv(), timeout=self._settings.request_timeout_s)
        parsed = parse_message(raw)
        if parsed.envelope.type != "server.hello_ack":
            raise FatalTransportError(f"expected server.hello_ack, got {parsed.envelope.type}")
        if parsed.envelope.corr_id != hello.id:
            raise FatalTransportError("hello_ack did not correlate with our hello")

        ack = parsed.payload
        assert isinstance(ack, HelloAck)
        if ack.protocol_version != PROTOCOL_VERSION:
            raise FatalTransportError(
                f"backend speaks protocol {ack.protocol_version}, agent speaks "
                f"{PROTOCOL_VERSION}; upgrade the agent"
            )

    async def _serve(self, websocket: ClientConnection, stop: asyncio.Event) -> None:
        receive = asyncio.ensure_future(self._receive_loop(websocket))
        waiter = asyncio.ensure_future(stop.wait())
        watcher = asyncio.ensure_future(self._watch_mode(websocket))
        sampler = asyncio.ensure_future(self._run_monitor(websocket, stop))
        try:
            done, _ = await asyncio.wait(
                {receive, waiter, watcher, sampler}, return_when=asyncio.FIRST_COMPLETED
            )
            if receive in done:
                receive.result()  # re-raise whatever ended the loop
            elif watcher in done:
                watcher.result()
            elif sampler in done:
                sampler.result()
            else:
                await websocket.close(1001)
        finally:
            for task in (receive, waiter, watcher, sampler):
                task.cancel()

    async def _run_monitor(self, websocket: ClientConnection, stop: asyncio.Event) -> None:
        """Stream activity metadata for as long as this connection lasts."""
        if self._monitor is None:
            await asyncio.Event().wait()  # nothing to sample; sleep forever
            return

        async def send_batch(batch: ActivityBatch) -> None:
            await websocket.send(build_envelope("agent.activity.batch", batch).to_json())

        async def send_telemetry(telemetry: SystemTelemetry) -> None:
            await websocket.send(build_envelope("agent.telemetry", telemetry).to_json())

        await self._monitor.run(send_batch=send_batch, send_telemetry=send_telemetry, stop=stop)

    async def _watch_mode(self, websocket: ClientConnection) -> None:
        """Tell the server when the mode changes.

        Polled rather than pushed: the mode can be flipped from a tray callback
        or a hotkey on another thread, and a 200 ms poll avoids marshalling
        those into the event loop for what is a single boolean.

        This is a *notification*. The agent does not wait for acknowledgement and
        does not care whether the server agrees — SAFE MODE is already in force
        locally by the time this is sent.
        """
        if self._safe_mode is None:
            await asyncio.Event().wait()  # nothing to watch; sleep forever
            return

        last = self._safe_mode.mode
        while True:
            await asyncio.sleep(0.2)
            change = self._safe_mode.current
            if change.mode is last:
                continue
            last = change.mode
            await websocket.send(
                build_envelope(
                    "agent.mode.changed",
                    ModeChanged(mode=change.mode, reason=change.reason),
                ).to_json()
            )
            log.info("agent_mode_reported", mode=change.mode.value, reason=change.reason)

    async def _receive_loop(self, websocket: ClientConnection) -> None:
        try:
            async for raw in websocket:
                await self._handle(raw, websocket)
        except websockets.exceptions.ConnectionClosed as exc:
            code = _close_code(exc)
            if code in _FATAL_CLOSE_CODES:
                raise FatalTransportError(_explain_close(code)) from exc
            raise

    async def _handle(self, raw: str | bytes, websocket: ClientConnection) -> None:
        try:
            parsed = parse_message(raw)
        except AtlasProtocolError as exc:
            # The backend sent something this agent cannot read. Log it and keep
            # the connection: dropping it would turn a single bad frame into an
            # outage.
            log.warning("agent_unparseable_frame", code=exc.code.value, error=exc.message)
            return

        message_type = parsed.envelope.type

        if message_type == "conn.ping":
            await websocket.send(
                build_envelope("conn.pong", ConnPong(), corr_id=parsed.envelope.id).to_json()
            )
            return

        if message_type == "server.error":
            payload = parsed.payload
            assert isinstance(payload, ErrorPayload)
            log.warning("agent_server_error", code=payload.code.value, message=payload.message)
            return

        # Anything that can act on this machine must carry a signature from the
        # key pinned at pairing time. Verified before the payload is looked at.
        if parsed.spec.signature_required:
            await self._handle_signed(parsed, websocket)
            return

        if self._on_message is not None:
            await self._on_message(parsed, websocket)
            return

        log.info("agent_unhandled_message", type=message_type)

    async def _handle_signed(self, parsed: ParsedMessage, websocket: ClientConnection) -> None:
        pinned = self._identity.server_public_key
        if pinned is None:
            # Paired before key pinning existed. Refusing is the only safe
            # reading: without a pinned key there is nothing to verify against.
            log.error("agent_no_pinned_server_key", type=parsed.envelope.type)
            await self._refuse(parsed, websocket, RefusalReason.UNKNOWN_SERVER_KEY)
            return

        try:
            require_signature(parsed, pinned)
        except AtlasProtocolError:
            log.error("agent_command_signature_invalid", type=parsed.envelope.type)
            await self._refuse(parsed, websocket, RefusalReason.SIGNATURE_INVALID)
            if self._safe_mode is not None:
                # A command that fails verification is either a bug or an
                # attack. Either way, stop accepting instructions.
                self._safe_mode.enter_safe_mode(
                    "a command failed signature verification", ModeChangeSource.AUTOMATIC
                )
            return

        # Only after the signature verifies, so an unsigned frame cannot poison
        # the seen-id cache with ids a real command might later use.
        try:
            self._replay.check(parsed.envelope)
        except AtlasProtocolError as exc:
            expired = "timestamp" in exc.message
            log.warning(
                "agent_command_rejected",
                type=parsed.envelope.type,
                reason=exc.message,
            )
            await self._refuse(
                parsed,
                websocket,
                RefusalReason.EXPIRED if expired else RefusalReason.REPLAYED,
            )
            return

        payload = parsed.payload

        if isinstance(payload, EnterSafeMode):
            if self._safe_mode is not None:
                self._safe_mode.enter_safe_mode(payload.reason, ModeChangeSource.REMOTE_REQUEST)
            return

        if isinstance(payload, ToolExecute):
            await self._execute(payload, parsed, websocket)
            return

        log.info("agent_unhandled_signed_message", type=parsed.envelope.type)

    async def _execute(
        self, command: ToolExecute, parsed: ParsedMessage, websocket: ClientConnection
    ) -> None:
        if self._runner is None:
            result = ToolResult(
                call_id=command.call_id,
                tool=command.tool,
                status=ToolStatus.NOT_IMPLEMENTED,
                duration_ms=0,
            )
        else:
            result = await self._runner.run(command)

        await self._send_result(result, parsed.envelope.id, websocket)

    async def _refuse(
        self, parsed: ParsedMessage, websocket: ClientConnection, reason: RefusalReason
    ) -> None:
        """Answer an unverifiable command, so the server is not left waiting."""
        call_id = str(parsed.envelope.payload.get("call_id") or new_ulid())
        tool = str(parsed.envelope.payload.get("tool") or parsed.envelope.type)
        await self._send_result(
            ToolResult(
                call_id=call_id,
                tool=tool,
                status=ToolStatus.REFUSED,
                refusal=reason,
                duration_ms=0,
            ),
            parsed.envelope.id,
            websocket,
        )

    async def _send_result(
        self, result: ToolResult, corr_id: str, websocket: ClientConnection
    ) -> None:
        envelope = build_envelope("agent.tool.result", result, corr_id=corr_id)
        # Signed with the device key: the audit trail records an outcome only
        # this machine could have produced.
        signed = sign_envelope(envelope, self._identity.private_key)
        await websocket.send(signed.to_json())

    def _next_delay(self, current: float, error: Exception) -> float:
        if _is_unauthorized(error):
            # The token expired or was rejected; a fresh one is cheap, so retry
            # promptly rather than backing off.
            return self._settings.reconnect_initial_s
        if _close_code_of(error) == _CLOSE_REPLACED:
            # Another agent instance took our place. Waiting longer avoids two
            # processes fighting over the connection.
            return self._settings.reconnect_replaced_s

        jitter = random.uniform(0.8, 1.2)  # noqa: S311 - jitter, not cryptography
        return min(current * 2 * jitter, self._settings.reconnect_max_s)


def _close_code(exc: websockets.exceptions.ConnectionClosed) -> int | None:
    for frame in (exc.rcvd, exc.sent):
        if frame is not None:
            return int(frame.code)
    return None


def _close_code_of(error: Exception) -> int | None:
    if isinstance(error, websockets.exceptions.ConnectionClosed):
        return _close_code(error)
    return None


def _is_unauthorized(error: Exception) -> bool:
    if _close_code_of(error) == _CLOSE_UNAUTHORIZED:
        return True
    status = getattr(getattr(error, "response", None), "status_code", None)
    return status in (401, 403)


def _explain_close(code: int | None) -> str:
    if code == _CLOSE_UNSUPPORTED_VERSION:
        return "backend rejected this agent's protocol version; upgrade the agent"
    if code == _CLOSE_REVOKED:
        return "this device has been revoked; pair it again to restore access"
    return f"connection closed with code {code}"
