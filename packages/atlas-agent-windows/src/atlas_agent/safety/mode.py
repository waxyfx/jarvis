"""SAFE MODE: the agent's own brake, which nothing remote can release.

The rule this module exists to enforce: **SAFE MODE can be entered from
anywhere, but only left from this machine.** The backend may ask the agent to
stop; it can never ask it to start again. A compromised server, a confused
model, or a stolen token can therefore take capability away from ATLAS and never
give it back — which is the asymmetry a kill switch needs in order to mean
anything.

State is persisted, so a restart does not quietly clear it. An agent that was
stopped for a reason stays stopped until a human on the keyboard says otherwise.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from atlas_shared.enums import AgentMode

__all__ = ["ModeChange", "ModeChangeSource", "SafeModeController", "SafeModeViolationError"]


class ModeChangeSource(StrEnum):
    """Where a mode change came from. Decides whether leaving is permitted."""

    #: Tray menu, on this machine.
    LOCAL_TRAY = "local_tray"
    #: Global hotkey, on this machine.
    LOCAL_HOTKEY = "local_hotkey"
    #: `atlas-agent safe-mode ...`, run on this machine.
    LOCAL_CLI = "local_cli"
    #: A signed request from the backend. May only ever tighten.
    REMOTE_REQUEST = "remote_request"
    #: The agent's own fail-safes: lost connection, repeated denials, a command
    #: whose signature did not verify.
    AUTOMATIC = "automatic"

    @property
    def is_local(self) -> bool:
        return self in (
            ModeChangeSource.LOCAL_TRAY,
            ModeChangeSource.LOCAL_HOTKEY,
            ModeChangeSource.LOCAL_CLI,
        )


class SafeModeViolationError(Exception):
    """Something tried to leave SAFE MODE without local authority."""


@dataclass(frozen=True, slots=True)
class ModeChange:
    mode: AgentMode
    reason: str
    source: ModeChangeSource
    at: datetime


class SafeModeController:
    """Owns the agent's mode. Thread-safe; the tray and hotkey run off-thread."""

    def __init__(
        self,
        state_path: Path,
        *,
        on_change: Callable[[ModeChange], None] | None = None,
    ) -> None:
        self._state_path = state_path
        self._on_change = on_change
        self._lock = threading.RLock()
        self._current = self._load()

    # ------------------------------------------------------------------ state

    @property
    def mode(self) -> AgentMode:
        with self._lock:
            return self._current.mode

    @property
    def is_safe(self) -> bool:
        return self.mode is AgentMode.SAFE

    @property
    def current(self) -> ModeChange:
        with self._lock:
            return self._current

    # ----------------------------------------------------------- transitions

    def enter_safe_mode(self, reason: str, source: ModeChangeSource) -> ModeChange:
        """Engage SAFE MODE. Permitted from any source, including remote.

        Idempotent: engaging while already engaged keeps the original reason,
        so the first cause of a shutdown is not overwritten by a later one.
        """
        with self._lock:
            if self._current.mode is AgentMode.SAFE:
                return self._current
            return self._apply(AgentMode.SAFE, reason, source)

    def leave_safe_mode(self, source: ModeChangeSource) -> ModeChange:
        """Return to normal operation. Local sources only.

        Raises:
            SafeModeViolationError: for any non-local source. This is the load-bearing
                check of the whole design — a remote caller must not be able to
                undo a kill switch.
        """
        if not source.is_local:
            raise SafeModeViolationError(
                f"SAFE MODE cannot be left from '{source}'; it requires physical "
                "access to this machine"
            )
        with self._lock:
            if self._current.mode is AgentMode.NORMAL:
                return self._current
            return self._apply(AgentMode.NORMAL, "released locally", source)

    def toggle(self, source: ModeChangeSource) -> ModeChange:
        """Flip the mode. Used by the tray and the hotkey."""
        if self.is_safe:
            return self.leave_safe_mode(source)
        return self.enter_safe_mode("engaged locally", source)

    # ------------------------------------------------------------ persistence

    def _apply(self, mode: AgentMode, reason: str, source: ModeChangeSource) -> ModeChange:
        change = ModeChange(mode=mode, reason=reason, source=source, at=datetime.now(UTC))
        self._current = change
        self._save(change)
        if self._on_change is not None:
            self._on_change(change)
        return change

    def _save(self, change: ModeChange) -> None:
        document = {
            "mode": change.mode.value,
            "reason": change.reason,
            "source": change.source.value,
            "at": change.at.isoformat(),
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(document, indent=2), encoding="utf-8")
        temporary.replace(self._state_path)

    def _load(self) -> ModeChange:
        normal = ModeChange(
            mode=AgentMode.NORMAL,
            reason="initial state",
            source=ModeChangeSource.AUTOMATIC,
            at=datetime.now(UTC),
        )
        if not self._state_path.exists():
            return normal

        try:
            document = json.loads(self._state_path.read_text(encoding="utf-8"))
            return ModeChange(
                mode=AgentMode(document["mode"]),
                reason=str(document.get("reason", "restored from disk")),
                source=ModeChangeSource(document.get("source", ModeChangeSource.AUTOMATIC)),
                at=datetime.fromisoformat(document["at"]),
            )
        except (OSError, ValueError, KeyError):
            # An unreadable state file must not silently mean "normal". Failing
            # into SAFE MODE is the conservative reading, and it is visible.
            return ModeChange(
                mode=AgentMode.SAFE,
                reason="mode state file is unreadable; failing safe",
                source=ModeChangeSource.AUTOMATIC,
                at=datetime.now(UTC),
            )
