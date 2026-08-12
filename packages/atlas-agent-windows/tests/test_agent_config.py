from pathlib import Path

import pytest
from pydantic import ValidationError

from atlas_agent.config import AgentSettings


def build(**overrides: object) -> AgentSettings:
    base: dict[str, object] = {
        "backend_url": "https://atlas.example.com",
        "identity_path": Path("unused-identity.json"),
    }
    return AgentSettings(**(base | overrides))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("backend_url", "expected"),
    [
        ("https://atlas.example.com", "wss://atlas.example.com/v1/ws"),
        ("http://127.0.0.1:8000", "ws://127.0.0.1:8000/v1/ws"),
        ("https://atlas.example.com/", "wss://atlas.example.com/v1/ws"),
        ("http://localhost:8000///", "ws://localhost:8000/v1/ws"),
    ],
)
def test_websocket_url_is_derived_from_backend_url(backend_url: str, expected: str) -> None:
    assert build(backend_url=backend_url).websocket_url == expected


@pytest.mark.parametrize("bad", ["atlas.example.com", "ftp://x", "wss://x", ""])
def test_backend_url_must_be_http(bad: str) -> None:
    with pytest.raises(ValidationError, match="must start with"):
        build(backend_url=bad)


def test_loopback_detection() -> None:
    assert build(backend_url="http://127.0.0.1:8000").is_loopback_backend is True
    assert build(backend_url="http://localhost:8000").is_loopback_backend is True
    assert build(backend_url="https://atlas.example.com").is_loopback_backend is False


def test_secure_defaults() -> None:
    settings = build()
    assert settings.verify_tls is True
    # Never store the key unprotected unless the operator says so explicitly.
    assert settings.allow_plaintext_key is False


def test_device_name_defaults_to_hostname() -> None:
    assert build().device_name


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_timeout_s", 0),
        ("request_timeout_s", 500),
        ("reconnect_initial_s", 0),
        ("reconnect_max_s", 100_000),
    ],
)
def test_out_of_range_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        build(**{field: value})
