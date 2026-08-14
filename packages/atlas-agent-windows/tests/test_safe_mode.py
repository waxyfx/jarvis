"""SAFE MODE: enterable from anywhere, leavable only from this machine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas_agent.safety.mode import (
    ModeChange,
    ModeChangeSource,
    SafeModeController,
    SafeModeViolationError,
)
from atlas_shared.enums import AgentMode

LOCAL_SOURCES = [
    ModeChangeSource.LOCAL_TRAY,
    ModeChangeSource.LOCAL_HOTKEY,
    ModeChangeSource.LOCAL_CLI,
]
REMOTE_SOURCES = [ModeChangeSource.REMOTE_REQUEST, ModeChangeSource.AUTOMATIC]


@pytest.fixture
def controller(tmp_path: Path) -> SafeModeController:
    return SafeModeController(tmp_path / "mode.json")


class TestDefaults:
    def test_starts_in_normal_mode(self, controller: SafeModeController) -> None:
        assert controller.mode is AgentMode.NORMAL
        assert controller.is_safe is False


class TestEntering:
    @pytest.mark.parametrize("source", [*LOCAL_SOURCES, *REMOTE_SOURCES])
    def test_any_source_may_engage(
        self, controller: SafeModeController, source: ModeChangeSource
    ) -> None:
        change = controller.enter_safe_mode("because", source)
        assert change.mode is AgentMode.SAFE
        assert controller.is_safe is True

    def test_engaging_twice_keeps_the_first_reason(self, controller: SafeModeController) -> None:
        controller.enter_safe_mode("kill switch pressed", ModeChangeSource.LOCAL_HOTKEY)
        controller.enter_safe_mode("connection lost", ModeChangeSource.AUTOMATIC)
        # The first cause of a shutdown is the interesting one; a later trigger
        # must not overwrite it.
        assert controller.current.reason == "kill switch pressed"
        assert controller.current.source is ModeChangeSource.LOCAL_HOTKEY


class TestLeaving:
    @pytest.mark.parametrize("source", LOCAL_SOURCES)
    def test_local_sources_may_release(
        self, controller: SafeModeController, source: ModeChangeSource
    ) -> None:
        controller.enter_safe_mode("test", ModeChangeSource.AUTOMATIC)
        change = controller.leave_safe_mode(source)
        assert change.mode is AgentMode.NORMAL
        assert controller.is_safe is False

    @pytest.mark.parametrize("source", REMOTE_SOURCES)
    def test_remote_sources_may_not_release(
        self, controller: SafeModeController, source: ModeChangeSource
    ) -> None:
        # The load-bearing rule of the whole design: a compromised backend can
        # take capability away and can never give it back.
        controller.enter_safe_mode("test", ModeChangeSource.LOCAL_TRAY)
        with pytest.raises(SafeModeViolationError, match="requires physical access"):
            controller.leave_safe_mode(source)
        assert controller.is_safe is True

    def test_leaving_when_already_normal_is_a_no_op(self, controller: SafeModeController) -> None:
        change = controller.leave_safe_mode(ModeChangeSource.LOCAL_CLI)
        assert change.mode is AgentMode.NORMAL


class TestToggle:
    def test_toggle_engages_then_releases(self, controller: SafeModeController) -> None:
        assert controller.toggle(ModeChangeSource.LOCAL_HOTKEY).mode is AgentMode.SAFE
        assert controller.toggle(ModeChangeSource.LOCAL_HOTKEY).mode is AgentMode.NORMAL

    def test_toggle_from_a_remote_source_cannot_release(
        self, controller: SafeModeController
    ) -> None:
        controller.enter_safe_mode("test", ModeChangeSource.AUTOMATIC)
        with pytest.raises(SafeModeViolationError):
            controller.toggle(ModeChangeSource.REMOTE_REQUEST)


class TestPersistence:
    def test_safe_mode_survives_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "mode.json"
        SafeModeController(path).enter_safe_mode("kill switch", ModeChangeSource.LOCAL_TRAY)

        # A restart must not quietly re-enable what was deliberately turned off.
        restarted = SafeModeController(path)
        assert restarted.is_safe is True
        assert restarted.current.reason == "kill switch"

    def test_normal_mode_survives_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "mode.json"
        controller = SafeModeController(path)
        controller.enter_safe_mode("test", ModeChangeSource.AUTOMATIC)
        controller.leave_safe_mode(ModeChangeSource.LOCAL_CLI)

        assert SafeModeController(path).is_safe is False

    def test_a_corrupt_state_file_fails_safe(self, tmp_path: Path) -> None:
        path = tmp_path / "mode.json"
        path.write_text("{ not json", encoding="utf-8")

        controller = SafeModeController(path)
        assert controller.is_safe is True
        assert "unreadable" in controller.current.reason

    def test_state_file_is_written_atomically(self, tmp_path: Path) -> None:
        path = tmp_path / "mode.json"
        controller = SafeModeController(path)
        controller.enter_safe_mode("test", ModeChangeSource.LOCAL_CLI)

        assert json.loads(path.read_text(encoding="utf-8"))["mode"] == "safe"
        assert not path.with_suffix(path.suffix + ".tmp").exists()


class TestNotifications:
    def test_observer_sees_every_change(self, tmp_path: Path) -> None:
        seen: list[ModeChange] = []
        controller = SafeModeController(tmp_path / "mode.json", on_change=seen.append)

        controller.enter_safe_mode("one", ModeChangeSource.LOCAL_TRAY)
        controller.leave_safe_mode(ModeChangeSource.LOCAL_TRAY)

        assert [change.mode for change in seen] == [AgentMode.SAFE, AgentMode.NORMAL]

    def test_no_notification_for_a_no_op(self, tmp_path: Path) -> None:
        seen: list[ModeChange] = []
        controller = SafeModeController(tmp_path / "mode.json", on_change=seen.append)

        controller.enter_safe_mode("one", ModeChangeSource.LOCAL_TRAY)
        controller.enter_safe_mode("two", ModeChangeSource.LOCAL_TRAY)
        assert len(seen) == 1


def test_source_classification() -> None:
    assert all(source.is_local for source in LOCAL_SOURCES)
    assert not any(source.is_local for source in REMOTE_SOURCES)
