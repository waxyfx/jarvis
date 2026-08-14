"""Runtime configuration.

Every setting is read from the environment with an ``ATLAS_`` prefix, so a
deployment is fully described by its ``.env`` file (see ``.env.example``).
Secrets are ``SecretStr`` so they cannot be printed by accident — including by
FastAPI's own error pages.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]

_MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ATLAS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"

    #: The owner record is created on the first bootstrap pairing, from these.
    owner_display_name: str = "Owner"
    owner_language: str = "ru"
    owner_timezone: str = "Asia/Almaty"

    #: SQLAlchemy async DSN, e.g. postgresql+asyncpg://user:pass@host:5432/atlas
    database_url: str
    database_echo: bool = False
    #: Disable connection pooling. Correct behind an external pooler such as
    #: pgbouncer, and used by the test suite so no connection outlives the
    #: event loop that opened it.
    database_use_null_pool: bool = False

    #: Signs access tokens. Symmetric is correct here: only this service issues
    #: and verifies them. Device identity, which crosses trust boundaries, is
    #: asymmetric instead.
    jwt_secret: SecretStr

    #: Ed25519 private key, base64url, 32 bytes. Signs commands sent to devices;
    #: each device pins the matching public key at pairing time. Changing it
    #: invalidates every pin and forces re-pairing. Generate one with the
    #: command shown in .env.example.
    server_signing_key: SecretStr

    #: Authorises the very first pairing, when no trusted device exists yet.
    #: Unset it after the Windows agent is paired.
    bootstrap_token: SecretStr | None = None

    access_token_ttl_s: int = Field(default=900, ge=60, le=3600)
    challenge_ttl_s: int = Field(default=120, ge=30, le=600)
    pairing_code_ttl_s: int = Field(default=300, ge=60, le=1800)

    #: How far an inbound envelope's timestamp may sit from server time.
    clock_skew_tolerance_s: int = Field(default=60, ge=5, le=300)

    heartbeat_interval_s: float = Field(default=30.0, gt=1.0, le=300.0)
    #: Missed heartbeats tolerated before the connection is dropped.
    heartbeat_grace_periods: int = Field(default=2, ge=1, le=10)
    #: How long a client has to send its hello after the socket opens.
    hello_timeout_s: float = Field(default=10.0, gt=0.5, le=60.0)

    #: Pairing attempts allowed per client address per minute.
    pairing_rate_limit_per_minute: int = Field(default=10, ge=1, le=120)

    #: Directories file tools may touch, as the *agent* sees them. Used for a
    #: cheap pre-filter; the agent re-checks with real path resolution and has
    #: the last word. Empty means no file tool can run — fail-safe, and the
    #: reason an unconfigured deployment cannot touch the disk by accident.
    allowed_file_roots: tuple[str, ...] = ()
    #: Directories an executable may legitimately live in. Anything outside is
    #: treated as an unknown binary and escalates to HIGH.
    allowed_executable_roots: tuple[str, ...] = (
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        r"C:\Windows",
    )

    #: How long to wait for an agent to answer a dispatched command.
    tool_dispatch_timeout_s: float = Field(default=60.0, gt=1.0, le=600.0)

    # ------------------------------------------------------------------ AI
    #: Gemini credentials. **Backend only** — never sent to the agent or the
    #: phone, never logged, never returned by any endpoint. Absent means the
    #: assistant endpoint reports that no model is configured, rather than
    #: pretending to work.
    gemini_api_key: SecretStr | None = None
    #: Verify against the current model list before deploying; ids change.
    gemini_model: str = "gemini-2.5-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    ai_request_timeout_s: float = Field(default=30.0, gt=1.0, le=300.0)

    #: Runaway-loop guards. A single user message may cause at most this many
    #: tool calls, across at most this many round trips to the model, within
    #: this much wall-clock time.
    ai_max_tool_calls_per_turn: int = Field(default=5, ge=1, le=20)
    ai_max_iterations: int = Field(default=3, ge=1, le=10)
    ai_turn_timeout_s: float = Field(default=90.0, gt=5.0, le=600.0)

    #: Hard daily stop, so a loop or a bad prompt cannot run up a bill unnoticed.
    ai_daily_token_budget: int = Field(default=2_000_000, ge=1000)

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level: {value}")
        return level

    @field_validator("server_signing_key")
    @classmethod
    def _check_signing_key(cls, value: SecretStr) -> SecretStr:
        from atlas_shared.crypto import KEY_SIZE, b64u_decode

        try:
            raw = b64u_decode(value.get_secret_value())
        except ValueError as exc:
            raise ValueError("server_signing_key must be base64url") from exc
        if len(raw) != KEY_SIZE:
            raise ValueError(f"server_signing_key must decode to {KEY_SIZE} bytes")
        return value

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use the postgresql+asyncpg:// driver")
        return value

    @model_validator(mode="after")
    def _enforce_production_hygiene(self) -> Settings:
        if self.environment != "prod":
            return self

        if len(self.jwt_secret.get_secret_value()) < _MIN_SECRET_LENGTH:
            raise ValueError(f"jwt_secret must be at least {_MIN_SECRET_LENGTH} characters in prod")
        if self.database_echo:
            # Statement logging would put device identifiers into the log stream.
            raise ValueError("database_echo must be off in prod")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()  # type: ignore[call-arg]
