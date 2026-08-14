"""The realtime endpoint — M1's acceptance criterion.

An agent must be able to authenticate, connect over WebSocket, complete the
hello handshake and be recorded in the audit log.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from atlas_backend.protocol_codes import CloseCode
from atlas_shared.crypto import generate_keypair
from atlas_shared.enums import AgentMode, DeviceKind, MessageKind, ToolStatus
from atlas_shared.ids import new_ulid
from atlas_shared.protocol.envelope import PROTOCOL_VERSION, Envelope, sign_envelope
from atlas_shared.protocol.messages import (
    AgentHello,
    ClientHello,
    ConnPing,
    HelloAck,
    ModeChanged,
    ToolResult,
    build_envelope,
    parse_message,
)
from tests.conftest import (
    authenticate,
    pair_device,
    paired_and_authenticated,
    requires_db,
    wait_for_sql,
)

pytestmark = [requires_db, pytest.mark.integration]


def agent_hello(mode: AgentMode = AgentMode.NORMAL) -> Envelope:
    return build_envelope(
        "agent.hello",
        AgentHello(
            agent_version="0.1.0",
            protocol_version=PROTOCOL_VERSION,
            platform="Windows-11",
            hostname="workstation",
            mode=mode,
            capabilities=("system", "apps"),
        ),
    )


def client_hello() -> Envelope:
    return build_envelope(
        "client.hello",
        ClientHello(app_version="0.1.0", protocol_version=PROTOCOL_VERSION, platform="iOS"),
    )


def connect(client: TestClient, token: str):  # type: ignore[no-untyped-def]
    return client.websocket_connect("/v1/ws", headers={"Authorization": f"Bearer {token}"})


def handshake(websocket, hello: Envelope | None = None) -> HelloAck:  # type: ignore[no-untyped-def]
    envelope = hello or agent_hello()
    websocket.send_text(envelope.to_json())
    parsed = parse_message(websocket.receive_text())
    assert parsed.envelope.type == "server.hello_ack"
    assert parsed.envelope.corr_id == envelope.id
    assert isinstance(parsed.payload, HelloAck)
    return parsed.payload


class TestAuthentication:
    def test_connection_without_a_token_is_refused(self, client: TestClient) -> None:
        pair_device(client)
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/v1/ws") as ws:
            ws.receive_text()

    @pytest.mark.parametrize("header", ["Bearer nonsense", "Basic abc", "Bearer "])
    def test_bad_credentials_are_refused(self, client: TestClient, header: str) -> None:
        pair_device(client)
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/v1/ws", headers={"Authorization": header}) as ws,
        ):
            ws.receive_text()

    def test_revoked_device_cannot_connect(self, client: TestClient) -> None:
        device, token = paired_and_authenticated(client)
        client.post(
            f"/v1/devices/{device.device_id}/revoke",
            headers={"Authorization": f"Bearer {token}"},
        )
        with pytest.raises(WebSocketDisconnect), connect(client, token) as ws:
            ws.receive_text()


class TestHandshake:
    def test_agent_completes_the_handshake(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        with connect(client, token) as ws:
            ack = handshake(ws)
            assert ack.protocol_version == PROTOCOL_VERSION
            assert ack.device_kind is DeviceKind.WINDOWS_AGENT
            assert ack.heartbeat_interval_s > 0

    def test_ios_client_completes_the_handshake(self, client: TestClient) -> None:
        first = pair_device(client)
        bearer = authenticate(client, first)
        phone = pair_device(client, kind=DeviceKind.IOS, name="iphone", bearer=bearer)
        phone_token = authenticate(client, phone)

        with connect(client, phone_token) as ws:
            ack = handshake(ws, client_hello())
            assert ack.device_kind is DeviceKind.IOS

    def test_wrong_hello_for_the_device_kind_is_refused(self, client: TestClient) -> None:
        # A Windows agent must not be able to present itself as a phone.
        _, token = paired_and_authenticated(client)
        with connect(client, token) as ws:
            ws.send_text(client_hello().to_json())
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
        assert exc.value.code == CloseCode.MALFORMED

    def test_duplicate_hello_is_refused(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        with connect(client, token) as ws:
            handshake(ws)
            ws.send_text(agent_hello().to_json())
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
        assert exc.value.code == CloseCode.MALFORMED

    def test_protocol_version_mismatch_in_payload_is_refused(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        frame = json.loads(agent_hello().to_json())
        frame["payload"]["protocol_version"] = 99

        with connect(client, token) as ws:
            ws.send_text(json.dumps(frame))
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
        assert exc.value.code == CloseCode.UNSUPPORTED_VERSION

    def test_envelope_version_mismatch_is_refused(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        frame = json.loads(agent_hello().to_json())
        frame["v"] = 99

        with connect(client, token) as ws:
            ws.send_text(json.dumps(frame))
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
        assert exc.value.code == CloseCode.UNSUPPORTED_VERSION

    def test_malformed_first_frame_is_refused(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        with connect(client, token) as ws:
            ws.send_text("this is not json")
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
        assert exc.value.code == CloseCode.MALFORMED

    def test_silence_before_hello_times_out(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        with connect(client, token) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
        assert exc.value.code == CloseCode.TIMEOUT


class TestMessaging:
    def test_ping_is_answered_with_pong(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        with connect(client, token) as ws:
            handshake(ws)
            ping = build_envelope("conn.ping", ConnPing())
            ws.send_text(ping.to_json())

            parsed = parse_message(ws.receive_text())
            assert parsed.envelope.type == "conn.pong"
            assert parsed.envelope.corr_id == ping.id

    def test_mode_change_is_audited(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        with connect(client, token) as ws:
            handshake(ws)
            ws.send_text(
                build_envelope(
                    "agent.mode.changed",
                    ModeChanged(mode=AgentMode.SAFE, reason="tray kill switch"),
                ).to_json()
            )
            # Round-trip a ping so the server has certainly processed the event.
            ws.send_text(build_envelope("conn.ping", ConnPing()).to_json())
            parse_message(ws.receive_text())

        rows = wait_for_sql("SELECT payload FROM audit_log WHERE event_type = 'agent.mode_changed'")
        assert rows and rows[0][0]["mode"] == "safe"

    def test_unknown_type_is_reported_without_closing(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        with connect(client, token) as ws:
            handshake(ws)
            frame = json.loads(build_envelope("conn.ping", ConnPing()).to_json())
            frame["type"] = "agent.invented_by_client"
            ws.send_text(json.dumps(frame))

            parsed = parse_message(ws.receive_text())
            assert parsed.envelope.kind is MessageKind.ERR
            assert parsed.payload.code == "unsupported_type"  # type: ignore[attr-defined]

            # Still usable afterwards.
            ws.send_text(build_envelope("conn.ping", ConnPing()).to_json())
            assert parse_message(ws.receive_text()).envelope.type == "conn.pong"

    def test_registered_but_unhandled_type_is_reported(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        with connect(client, token) as ws:
            handshake(ws)
            frame = json.loads(build_envelope("conn.ping", ConnPing()).to_json())
            frame["type"] = "server.hello_ack"
            frame["kind"] = "res"
            frame["payload"] = {
                "server_version": "0.1.0",
                "protocol_version": PROTOCOL_VERSION,
                "session_id": "x",
                "device_kind": "ios",
                "server_time": "2026-08-12T10:00:00.000Z",
                "heartbeat_interval_s": 30.0,
            }
            ws.send_text(json.dumps(frame))

            parsed = parse_message(ws.receive_text())
            assert parsed.envelope.kind is MessageKind.ERR
            assert parsed.payload.code == "unsupported_type"  # type: ignore[attr-defined]

    def test_replayed_envelope_id_closes_the_connection(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        with connect(client, token) as ws:
            handshake(ws)
            ping = build_envelope("conn.ping", ConnPing())
            ws.send_text(ping.to_json())
            parse_message(ws.receive_text())

            ws.send_text(ping.to_json())
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
        assert exc.value.code == CloseCode.REPLAY

    def test_stale_timestamp_closes_the_connection(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        with connect(client, token) as ws:
            handshake(ws)
            frame = json.loads(build_envelope("conn.ping", ConnPing()).to_json())
            frame["ts"] = "2020-01-01T00:00:00.000Z"
            frame["id"] = new_ulid()
            ws.send_text(json.dumps(frame))
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
        assert exc.value.code == CloseCode.REPLAY


class TestConnectionLifecycle:
    def test_second_connection_replaces_the_first(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        with connect(client, token) as first:
            handshake(first)
            with connect(client, token) as second:
                handshake(second)
                with pytest.raises(WebSocketDisconnect) as exc:
                    first.receive_text()
        assert exc.value.code == CloseCode.REPLACED

    def test_connection_open_is_audited_and_session_recorded(self, client: TestClient) -> None:
        """Opening side only.

        The close path is not asserted here: ``TestClient`` cancels the ASGI
        task when the websocket context exits, so the handler's ``finally``
        never finishes writing. That is a property of the test client, not of
        the server — ``tests/test_end_to_end.py`` covers close against a real
        uvicorn process.
        """
        device, token = paired_and_authenticated(client)
        with connect(client, token) as ws:
            handshake(ws)

            assert wait_for_sql("SELECT seq FROM audit_log WHERE event_type = 'connection.opened'")
            # Filter on the flag rather than reading it: the row exists before
            # the handshake is recorded, so a bare SELECT would satisfy
            # wait_for_sql immediately and read the value mid-flight.
            sessions = wait_for_sql(
                "SELECT handshake_ok FROM device_sessions "
                "WHERE device_id = :device_id AND handshake_ok IS TRUE",
                device_id=device.device_id,
            )
            assert sessions == [(True,)]

    def test_revocation_drops_a_live_connection(self, client: TestClient) -> None:
        device, token = paired_and_authenticated(client)
        with connect(client, token) as ws:
            handshake(ws)
            client.post(
                f"/v1/devices/{device.device_id}/revoke",
                headers={"Authorization": f"Bearer {token}"},
            )
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
        assert exc.value.code == CloseCode.REVOKED

    def test_device_shows_as_connected_while_the_socket_is_open(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        headers = {"Authorization": f"Bearer {token}"}

        assert client.get("/v1/devices", headers=headers).json()[0]["connected"] is False
        with connect(client, token) as ws:
            handshake(ws)
            listed = client.get("/v1/devices", headers=headers).json()
            assert listed[0]["connected"] is True


class TestToolResultSignatures:
    """A result is only believed if the device that ran it signed it."""

    @staticmethod
    def _result_envelope() -> Envelope:
        return build_envelope(
            "agent.tool.result",
            ToolResult(
                call_id=new_ulid(),
                tool="system.metrics",
                status=ToolStatus.OK,
                result={"ram_total_mb": 1},
                duration_ms=1,
            ),
            corr_id=new_ulid(),
        )

    def test_a_result_signed_by_a_foreign_key_is_refused(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        attacker_private, _ = generate_keypair()

        with connect(client, token) as ws:
            handshake(ws)
            ws.send_text(sign_envelope(self._result_envelope(), attacker_private).to_json())

            parsed = parse_message(ws.receive_text())
            assert parsed.envelope.kind is MessageKind.ERR
            assert parsed.payload.code == "signature_invalid"  # type: ignore[attr-defined]

        assert wait_for_sql("SELECT seq FROM audit_log WHERE event_type = 'tool.result_unverified'")

    def test_an_unsigned_result_is_refused(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)

        with connect(client, token) as ws:
            handshake(ws)
            ws.send_text(self._result_envelope().to_json())

            parsed = parse_message(ws.receive_text())
            assert parsed.envelope.kind is MessageKind.ERR
            assert parsed.payload.code == "signature_invalid"  # type: ignore[attr-defined]

    def test_a_tampered_result_is_refused(self, client: TestClient) -> None:
        device, token = paired_and_authenticated(client)

        with connect(client, token) as ws:
            handshake(ws)
            signed = sign_envelope(self._result_envelope(), device.private_key)
            tampered = signed.model_copy(update={"payload": {**signed.payload, "status": "error"}})
            ws.send_text(tampered.to_json())

            parsed = parse_message(ws.receive_text())
            assert parsed.payload.code == "signature_invalid"  # type: ignore[attr-defined]

    def test_a_correctly_signed_result_is_accepted(self, client: TestClient) -> None:
        device, token = paired_and_authenticated(client)

        with connect(client, token) as ws:
            handshake(ws)
            ws.send_text(sign_envelope(self._result_envelope(), device.private_key).to_json())

            # Nothing is waiting for this correlation id, so the server drops it
            # quietly. What matters is that no error came back and the socket is
            # still usable — the signature was accepted.
            ping = build_envelope("conn.ping", ConnPing())
            ws.send_text(ping.to_json())
            answer = parse_message(ws.receive_text())
            assert answer.envelope.type == "conn.pong"


class TestHeartbeat:
    def test_server_pings_an_idle_connection(self, client: TestClient) -> None:
        _, token = paired_and_authenticated(client)
        with connect(client, token) as ws:
            handshake(ws)
            # heartbeat_interval_s is 2.0 in the test settings.
            parsed = parse_message(ws.receive_text())
            assert parsed.envelope.type == "conn.ping"
