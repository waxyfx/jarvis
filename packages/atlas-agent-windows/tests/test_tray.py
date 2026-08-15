"""Tray and hotkey: the local controls that work without a backend."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from atlas_agent.config import AgentSettings
from atlas_agent.monitor import ActivityMonitor
from atlas_agent.safety.mode import ModeChangeSource, SafeModeController
from atlas_agent.tray import GlobalHotkey, TrayApplication

on_windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only UI")


@pytest.fixture
def controller(tmp_path: Path) -> SafeModeController:
    return SafeModeController(tmp_path / "mode.json")


@pytest.fixture
def monitor() -> ActivityMonitor:
    return ActivityMonitor(
        AgentSettings(backend_url="http://127.0.0.1:8000", identity_path=Path("unused.json"))
    )


@pytest.fixture
def tray(controller: SafeModeController, monitor: ActivityMonitor) -> TrayApplication:
    return TrayApplication(safe_mode=controller, monitor=monitor)


class TestStatusText:
    def test_active_state(self, tray: TrayApplication) -> None:
        assert tray._title() == "JARVIS — active"

    def test_safe_mode_is_visible(
        self, tray: TrayApplication, controller: SafeModeController
    ) -> None:
        controller.enter_safe_mode("test", ModeChangeSource.LOCAL_TRAY)
        assert "SAFE MODE" in tray._title()

    def test_paused_monitoring_is_visible(
        self, tray: TrayApplication, monitor: ActivityMonitor
    ) -> None:
        monitor.pause()
        assert "monitoring paused" in tray._title()


class TestLocalControls:
    def test_toggling_safe_mode_from_the_tray_works_offline(
        self, tray: TrayApplication, controller: SafeModeController
    ) -> None:
        # No backend, no token, no network anywhere in this test — which is the
        # property that matters for a kill switch.
        tray._toggle_safe_mode()
        assert controller.is_safe is True

        tray._toggle_safe_mode()
        assert controller.is_safe is False

    def test_the_tray_source_counts_as_local(
        self, tray: TrayApplication, controller: SafeModeController
    ) -> None:
        controller.enter_safe_mode("test", ModeChangeSource.REMOTE_REQUEST)
        tray._toggle_safe_mode()
        # A remotely engaged SAFE MODE can still be released by a person here.
        assert controller.is_safe is False

    def test_toggling_monitoring(self, tray: TrayApplication, monitor: ActivityMonitor) -> None:
        tray._toggle_monitor()
        assert monitor.paused is True
        tray._toggle_monitor()
        assert monitor.paused is False

    def test_quit_calls_back(self, controller: SafeModeController) -> None:
        called: list[bool] = []
        tray = TrayApplication(safe_mode=controller, on_quit=lambda: called.append(True))
        tray._quit()
        assert called == [True]


@on_windows
class TestIcon:
    def test_availability(self, tray: TrayApplication) -> None:
        assert tray.available() is True

    def test_colour_reflects_mode(
        self, tray: TrayApplication, controller: SafeModeController
    ) -> None:
        active = tray._image().getpixel((32, 32))
        controller.enter_safe_mode("test", ModeChangeSource.LOCAL_TRAY)
        safe = tray._image().getpixel((32, 32))
        # Also distinguishable without colour: SAFE MODE draws a bar across the
        # centre, so the two centre pixels differ for that reason too.
        assert active != safe

    def test_menu_builds(self, tray: TrayApplication) -> None:
        assert len(list(tray._menu())) >= 5


@on_windows
class TestGlobalHotkey:
    def test_registers_and_releases(self) -> None:
        pressed = threading.Event()
        # F24: nothing else claims it, so this cannot collide with a real
        # application's shortcut while the suite runs.
        hotkey = GlobalHotkey(pressed.set, modifiers=0, key=0x87)

        assert hotkey.start() is True
        assert hotkey.is_registered is True
        hotkey.stop()
        assert hotkey.is_registered is False

    def test_a_second_registration_of_the_same_key_is_refused(self) -> None:
        first = GlobalHotkey(lambda: None, modifiers=0, key=0x87)
        assert first.start() is True
        try:
            second = GlobalHotkey(lambda: None, modifiers=0, key=0x87)
            # Windows allows only one owner per combination. Reporting failure
            # is what lets the agent log "hotkey unavailable" instead of
            # pretending the kill switch is armed.
            assert second.start() is False
            second.stop()
        finally:
            first.stop()

    def test_stopping_an_unstarted_hotkey_is_safe(self) -> None:
        GlobalHotkey(lambda: None).stop()


def test_tray_absence_is_not_fatal(
    controller: SafeModeController, monkeypatch: pytest.MonkeyPatch
) -> None:
    tray = TrayApplication(safe_mode=controller)
    monkeypatch.setattr(TrayApplication, "available", lambda _self: False)

    # A missing icon must never be the reason a kill switch is missing: run()
    # reports failure, the agent carries on, and the CLI still works.
    assert tray.run() is False
    controller.enter_safe_mode("cli", ModeChangeSource.LOCAL_CLI)
    assert controller.is_safe is True


@on_windows
def test_hotkey_engages_safe_mode_end_to_end(controller: SafeModeController) -> None:
    """The wiring the CLI uses, exercised without the CLI."""

    def engage() -> None:
        controller.toggle(ModeChangeSource.LOCAL_HOTKEY)

    hotkey = GlobalHotkey(engage, modifiers=0, key=0x87)
    assert hotkey.start() is True
    try:
        # Simulate the keypress by invoking what the message loop would invoke;
        # synthesising a real WM_HOTKEY would test Windows, not ATLAS.
        engage()
        time.sleep(0.05)
        assert controller.is_safe is True
        assert controller.current.source is ModeChangeSource.LOCAL_HOTKEY
    finally:
        hotkey.stop()
