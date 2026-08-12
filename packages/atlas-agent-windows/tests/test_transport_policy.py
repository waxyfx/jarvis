"""Reconnection policy: which close codes mean retry, wait, or stop."""

from __future__ import annotations

from pathlib import Path

import pytest
import websockets
from websockets.frames import Close

from atlas_agent.config import AgentSettings
from atlas_agent.identity import DeviceIdentity
from atlas_agent.transport import (
    _CLOSE_REPLACED,
    _CLOSE_REVOKED,
    _CLOSE_UNAUTHORIZED,
    _CLOSE_UNSUPPORTED_VERSION,
    AgentTransport,
    _close_code,
    _explain_close,
    _is_unauthorized,
)


def settings(**overrides: object) -> AgentSettings:
    base: dict[str, object] = {
        "backend_url": "http://127.0.0.1:8000",
        "identity_path": Path("unused-identity.json"),
        "reconnect_initial_s": 1.0,
        "reconnect_max_s": 60.0,
        "reconnect_replaced_s": 30.0,
    }
    return AgentSettings(**(base | overrides))  # type: ignore[arg-type]


def transport(**overrides: object) -> AgentTransport:
    identity = DeviceIdentity(
        private_key=b"\x01" * 32, public_key=b"\x02" * 32, device_id="device-1"
    )
    return AgentTransport(settings(**overrides), identity)


def closed(code: int) -> websockets.exceptions.ConnectionClosed:
    return websockets.exceptions.ConnectionClosed(Close(code, ""), None)


class TestCloseCodes:
    def test_code_is_read_from_the_received_frame(self) -> None:
        assert _close_code(closed(_CLOSE_REVOKED)) == _CLOSE_REVOKED

    def test_missing_frames_yield_none(self) -> None:
        assert _close_code(websockets.exceptions.ConnectionClosed(None, None)) is None

    def test_unauthorized_is_recognised(self) -> None:
        assert _is_unauthorized(closed(_CLOSE_UNAUTHORIZED)) is True
        assert _is_unauthorized(closed(_CLOSE_REPLACED)) is False

    @pytest.mark.parametrize(
        ("code", "fragment"),
        [
            (_CLOSE_UNSUPPORTED_VERSION, "upgrade the agent"),
            (_CLOSE_REVOKED, "pair it again"),
            (1006, "1006"),
        ],
    )
    def test_explanations_are_actionable(self, code: int, fragment: str) -> None:
        assert fragment in _explain_close(code)


class TestBackoff:
    def test_unauthorized_retries_immediately(self) -> None:
        # A rejected token is cheap to replace, so backing off would only add
        # downtime.
        agent = transport()
        assert agent._next_delay(30.0, closed(_CLOSE_UNAUTHORIZED)) == 1.0

    def test_being_replaced_waits_longer(self) -> None:
        agent = transport()
        assert agent._next_delay(1.0, closed(_CLOSE_REPLACED)) == 30.0

    def test_generic_failures_grow_the_delay(self) -> None:
        agent = transport()
        delay = agent._next_delay(1.0, OSError("network down"))
        assert 1.6 <= delay <= 2.4  # doubling, with jitter

    def test_delay_is_capped(self) -> None:
        agent = transport(reconnect_max_s=10.0)
        assert agent._next_delay(1000.0, OSError("network down")) == 10.0

    def test_repeated_failures_converge_on_the_cap(self) -> None:
        agent = transport(reconnect_max_s=10.0)
        delay = 1.0
        for _ in range(20):
            delay = agent._next_delay(delay, OSError("network down"))
        assert delay == 10.0


def test_http_status_errors_count_as_unauthorized() -> None:
    class FakeResponse:
        status_code = 401

    class FakeError(Exception):
        response = FakeResponse()

    assert _is_unauthorized(FakeError()) is True
