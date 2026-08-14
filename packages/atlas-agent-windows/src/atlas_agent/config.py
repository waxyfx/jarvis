"""Agent configuration.

Read from the environment with an ``ATLAS_AGENT_`` prefix, or from
``.env.agent`` next to the working directory. The agent holds no secrets in
configuration: its only credential is the private key, which lives in the
identity file under DPAPI.
"""

from __future__ import annotations

import os
import socket
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["AgentSettings", "get_agent_settings"]


def _state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.config")
    return Path(base) / "ATLAS"


def _default_identity_path() -> Path:
    return _state_dir() / "agent_identity.json"


def _default_mode_path() -> Path:
    return _state_dir() / "safe_mode.json"


def _default_file_roots() -> tuple[str, ...]:
    """Where file tools may work by default.

    Conservative on purpose: the three folders a person actually asks an
    assistant about. Everything else — including the rest of the user profile —
    is out of bounds until deliberately added.
    """
    home = Path.home()
    return tuple(
        str(home / name) for name in ("Desktop", "Downloads", "Documents") if (home / name).is_dir()
    )


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ATLAS_AGENT_",
        env_file=".env.agent",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    #: Base URL of the backend, e.g. https://atlas.example.com
    backend_url: str = "http://127.0.0.1:8000"
    device_name: str = Field(default_factory=socket.gethostname)
    identity_path: Path = Field(default_factory=_default_identity_path)
    #: SAFE MODE state. Local file: the kill switch works with no network.
    mode_state_path: Path = Field(default_factory=_default_mode_path)

    #: Directories file tools may touch. The agent resolves paths for real and
    #: has the last word; the backend's copy of this list is only a pre-filter.
    allowed_file_roots: tuple[str, ...] = Field(default_factory=_default_file_roots)
    allowed_executable_roots: tuple[str, ...] = (
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        r"C:\Windows",
    )
    #: Extra glob patterns that are never accessible, on top of the built-in
    #: floor in atlas_agent.safety.paths.
    denied_path_patterns: tuple[str, ...] = ()

    enable_tray: bool = True
    enable_hotkey: bool = True

    #: Refuse to store the private key unprotected. Only meaningful off Windows,
    #: where DPAPI is unavailable; leaving this False is the safe default.
    allow_plaintext_key: bool = False

    request_timeout_s: float = Field(default=15.0, gt=0, le=120)

    #: How far a signed command's timestamp may sit from local time before the
    #: agent refuses it. Bounds how long a captured command stays usable.
    command_freshness_s: int = Field(default=120, ge=10, le=600)

    #: Activity sampling. Metadata only — see atlas_agent.monitor.
    monitor_enabled: bool = True
    monitor_interval_s: float = Field(default=10.0, ge=1.0, le=300.0)
    monitor_batch_size: int = Field(default=12, ge=1, le=200)
    #: Seconds without keyboard or mouse input before the user counts as idle.
    monitor_idle_threshold_s: int = Field(default=60, ge=15, le=3600)
    telemetry_interval_s: float = Field(default=60.0, ge=10.0, le=3600.0)
    reconnect_initial_s: float = Field(default=1.0, gt=0, le=60)
    reconnect_max_s: float = Field(default=60.0, gt=0, le=3600)
    #: Extra wait after being displaced by another connection for this device.
    reconnect_replaced_s: float = Field(default=30.0, gt=0, le=600)

    verify_tls: bool = True
    log_level: str = "INFO"

    @field_validator("backend_url")
    @classmethod
    def _normalise_backend_url(cls, value: str) -> str:
        stripped = value.rstrip("/")
        if not stripped.startswith(("http://", "https://")):
            raise ValueError("backend_url must start with http:// or https://")
        return stripped

    @property
    def websocket_url(self) -> str:
        """The realtime endpoint, derived from :attr:`backend_url`."""
        scheme, _, rest = self.backend_url.partition("://")
        websocket_scheme = "wss" if scheme == "https" else "ws"
        return f"{websocket_scheme}://{rest}/v1/ws"

    @property
    def is_loopback_backend(self) -> bool:
        return "://127.0.0.1" in self.backend_url or "://localhost" in self.backend_url


@lru_cache(maxsize=1)
def get_agent_settings() -> AgentSettings:
    return AgentSettings()
