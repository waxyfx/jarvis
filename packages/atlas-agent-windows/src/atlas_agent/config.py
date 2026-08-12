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


def _default_identity_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.config")
    return Path(base) / "ATLAS" / "agent_identity.json"


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

    #: Refuse to store the private key unprotected. Only meaningful off Windows,
    #: where DPAPI is unavailable; leaving this False is the safe default.
    allow_plaintext_key: bool = False

    request_timeout_s: float = Field(default=15.0, gt=0, le=120)
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
