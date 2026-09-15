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
    #: Load-bearing since M5: everything the proactive rules decide is in local
    #: time — "today", "this morning", "after nine in the evening" — and UTC
    #: would put the morning briefing in the middle of the night.
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
    #: An alias rather than a pinned id, on purpose. A pinned model eventually
    #: stops being served — `gemini-2.5-flash` returned 404 with "no longer
    #: available to new users" while still appearing in the model list — and the
    #: failure looks like a broken assistant. The alias tracks the current flash
    #: model; pin a specific id here if you need reproducibility, and let
    #: e2e/test_gemini_live.py tell you when behaviour shifts.
    gemini_model: str = "gemini-flash-latest"
    #: Tried when the first model answers 503 or 429 rather than an error about
    #: the request. Measured cause: `gemini-flash-latest` returned "experiencing
    #: high demand" for several minutes, which took every scenario down at once.
    #: Empty disables the fallback, which is a supported configuration.
    gemini_fallback_model: str = "gemini-flash-lite-latest"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"

    # --------------------------------------------------------------- tracker
    #: The owner's Life OS, reached over HTTP. **Backend only**, for the same
    #: reason the Gemini key is: it is a credential for a remote service with
    #: write access to their data, and it has no business on a laptop or a
    #: phone.
    #:
    #: Absent either of these the tracker is unavailable, its tools are not
    #: offered to the model, and nothing is constructed — off by default, as
    #: Sunny's own side is.
    #:
    #: The URL is configuration rather than a constant so a local instance can
    #: be used in development without a deployed backend ever being pointed at
    #: someone's `localhost`.
    sunny_base_url: str = ""
    sunny_token: SecretStr | None = None
    sunny_timeout_s: float = Field(default=15.0, gt=1.0, le=60.0)

    # ------------------------------------------------------------------- web
    #: Whether the assistant may look things up on the internet.
    #:
    #: On by default, unlike the tracker, because there is no credential to
    #: configure and nothing to leak — the search provider needs no account.
    #: The switch exists for the case where this backend should not be reaching
    #: outward at all, and turning it off removes the web tools from what the
    #: model is offered rather than letting it call them and be refused.
    web_tools_enabled: bool = True

    # ------------------------------------------------- speaking first (M5)
    #: Whether the assistant may start a conversation: reminders, the morning
    #: briefing, the evening summary, a nudge after three hours at the desk.
    #:
    #: On by default, because an assistant that only answers when spoken to is
    #: half of what was asked for. Off is a single flag, because the failure
    #: mode of this feature is being annoying, and someone who finds it annoying
    #: should not have to hunt.
    proactive_enabled: bool = True
    #: How often the rules get a chance to fire. A minute is the resolution of
    #: "remind me fifteen minutes before", and there is nothing here worth
    #: knowing sooner.
    proactive_interval_s: float = Field(default=60.0, ge=10.0, le=3600.0)

    #: Windows, not thresholds — see atlas_backend/notify/rules.py. Sitting
    #: down at four in the afternoon should not produce a morning briefing.
    briefing_hour: int = Field(default=8, ge=0, le=23)
    briefing_until_hour: int = Field(default=12, ge=1, le=24)
    evening_summary_hour: int = Field(default=21, ge=0, le=23)
    evening_summary_until_hour: int = Field(default=23, ge=1, le=24)
    long_session_minutes: int = Field(default=90, ge=20, le=480)
    #: Outside these hours a notification is shown but not spoken. See
    #: atlas_backend/notify/rules.py for why that is a downgrade rather than a
    #: filter.
    quiet_from_hour: int = Field(default=23, ge=0, le=23)
    quiet_until_hour: int = Field(default=7, ge=0, le=23)

    # ------------------------------------------------- daily report (M5)
    #: The day written down as a note in the tracker: which tasks were done,
    #: which were not, how the time at the computer went.
    #:
    #: Needs a tracker that can file a note, so it is off in practice when
    #: nothing is configured — the flag only decides whether to try.
    daily_report_enabled: bool = True
    #: Late enough to be the end of the day, early enough that the owner is
    #: likely still awake to see it appear. Unlike the notifications, it does
    #: not require anyone to be at the machine.
    daily_report_hour: int = Field(default=22, ge=0, le=23)

    # ------------------------------------------------------- prayer times (M5)
    #: Computed on this machine, never fetched. See atlas_backend/prayer/times.py
    #: for why, and for why the method below is a setting rather than a constant.
    #:
    #: Off unless coordinates are given: prayer times for the wrong city are
    #: worse than none, and guessing them from the timezone would be a guess.
    prayer_enabled: bool = True
    prayer_latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    prayer_longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    #: One of the keys in atlas_backend.prayer.times.METHODS. The angles differ
    #: between authorities by twenty minutes or more at this latitude, so this
    #: is worth checking against a local timetable once.
    prayer_method: str = "mwl"
    #: "standard" (Shafi'i and others) or "hanafi", which is about an hour later.
    prayer_asr: str = "standard"
    #: How long before each prayer to say something. Zero switches the
    #: reminders off while leaving the question answerable.
    prayer_reminder_minutes: int = Field(default=10, ge=0, le=60)

    # --------------------------------------------------- personality (M5)
    #: How replies are worded, once everything about *what* they say has been
    #: decided. The layer can add at most a short preface and can never change a
    #: fact, reach a tool, or touch a policy decision - see
    #: atlas_backend/personality/engine.py.
    #:
    #: On by default in "jarvis" mode, which is the assistant the owner asked
    #: for. "professional" switches the decoration off entirely.
    personality_enabled: bool = True
    #: professional | jarvis | personal | custom
    personality_mode: str = "jarvis"
    #: auto | none | sir
    personality_address: str = "auto"
    ai_request_timeout_s: float = Field(default=30.0, gt=1.0, le=300.0)
    #: Retries for transient upstream failures (429, 5xx). A rate limit is a
    #: "wait a moment", not a "cannot do that" — telling the user the model is
    #: unavailable when one retry would have worked is a worse answer than the
    #: delay. Bounded so it cannot become a source of latency or cost.
    ai_max_retries: int = Field(default=2, ge=0, le=5)
    ai_retry_base_delay_s: float = Field(default=2.0, gt=0.1, le=30.0)

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
