"""Tray icon and global kill-switch hotkey.

Both are *local* controls. They call :class:`SafeModeController` directly, which
writes a file on this disk — no network, no backend, no token. Engaging SAFE
MODE therefore works when the connection is down, when the backend is
unreachable, and when the backend is actively hostile. That is the entire point
of putting the switch here rather than in the API.

The tray is optional: if the GUI libraries are unavailable the agent still runs
headless and the hotkey and CLI still work. A missing icon must never be the
reason a kill switch is missing.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

from atlas_agent.logging import get_logger
from atlas_agent.monitor import ActivityMonitor
from atlas_agent.safety.mode import ModeChangeSource, SafeModeController
from atlas_shared.enums import AgentMode

__all__ = ["DEFAULT_HOTKEY", "GlobalHotkey", "TrayApplication"]

log = get_logger(__name__)

_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_NOREPEAT = 0x4000

#: Ctrl+Alt+Shift+A. Three modifiers so it cannot be hit by accident, and a
#: combination no common application claims.
DEFAULT_HOTKEY = (_MOD_CONTROL | _MOD_ALT | _MOD_SHIFT | _MOD_NOREPEAT, ord("A"))
DEFAULT_HOTKEY_LABEL = "Ctrl+Alt+Shift+A"


class GlobalHotkey:
    """A system-wide hotkey, served by its own thread and message loop.

    ``RegisterHotKey`` binds to the calling thread and delivers ``WM_HOTKEY``
    through that thread's message queue, so the loop has to live somewhere that
    is not the asyncio event loop.
    """

    def __init__(
        self,
        callback: Callable[[], None],
        *,
        modifiers: int = DEFAULT_HOTKEY[0],
        key: int = DEFAULT_HOTKEY[1],
    ) -> None:
        self._callback = callback
        self._modifiers = modifiers
        self._key = key
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._registered = threading.Event()
        self._ok = False

    @property
    def is_registered(self) -> bool:
        return self._ok

    def start(self, *, timeout_s: float = 5.0) -> bool:
        """Register the hotkey. Returns False if the OS refused it."""
        if sys.platform != "win32":
            return False

        self._thread = threading.Thread(target=self._run, name="atlas-hotkey", daemon=True)
        self._thread.start()
        self._registered.wait(timeout=timeout_s)
        return self._ok

    def stop(self) -> None:
        if self._thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        if not user32.RegisterHotKey(None, 1, self._modifiers, self._key):
            log.warning(
                "hotkey_unavailable",
                hotkey=DEFAULT_HOTKEY_LABEL,
                hint="another application already owns this combination",
            )
            self._registered.set()
            return

        self._ok = True
        self._registered.set()
        log.info("hotkey_registered", hotkey=DEFAULT_HOTKEY_LABEL)

        message = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == _WM_HOTKEY:
                    try:
                        self._callback()
                    except Exception:
                        # take the kill switch down with it
                        log.exception("hotkey_callback_failed")
        finally:
            user32.UnregisterHotKey(None, 1)
            self._ok = False


class TrayApplication:
    """Status and local controls in the notification area."""

    def __init__(
        self,
        *,
        safe_mode: SafeModeController,
        monitor: ActivityMonitor | None = None,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        self._safe_mode = safe_mode
        self._monitor = monitor
        self._on_quit = on_quit
        # pystray ships no type information, so the icon and the objects built
        # from it are Any by necessity rather than by laziness.
        self._icon: Any = None

    def available(self) -> bool:
        try:
            import pystray  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            return False
        return sys.platform == "win32"

    def run(self) -> bool:
        """Show the icon and block until quit. False if the tray is unavailable."""
        if not self.available():
            log.info("tray_unavailable", hint="agent continues headless; hotkey and CLI still work")
            return False

        import pystray

        self._icon = pystray.Icon(
            "atlas",
            icon=self._image(),
            title=self._title(),
            menu=self._menu(),
        )
        self._icon.run()
        return True

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()

    def refresh(self) -> None:
        """Redraw after a state change made elsewhere (hotkey, CLI, backend)."""
        if self._icon is None:
            return
        self._icon.icon = self._image()
        self._icon.title = self._title()
        self._icon.update_menu()

    # ------------------------------------------------------------------ menu

    def _title(self) -> str:
        state = "SAFE MODE" if self._safe_mode.is_safe else "active"
        if self._monitor is not None and self._monitor.paused:
            state += " · monitoring paused"
        return f"JARVIS — {state}"

    def _menu(self) -> Any:
        import pystray

        return pystray.Menu(
            pystray.MenuItem(lambda _: self._title(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "SAFE MODE (kill switch)",
                self._toggle_safe_mode,
                checked=lambda _: self._safe_mode.is_safe,
            ),
            pystray.MenuItem(
                "Pause activity monitoring",
                self._toggle_monitor,
                checked=lambda _: self._monitor is not None and self._monitor.paused,
                enabled=self._monitor is not None,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Kill switch: {DEFAULT_HOTKEY_LABEL}", None, enabled=False),
            pystray.MenuItem("Quit JARVIS agent", self._quit),
        )

    def _toggle_safe_mode(self) -> None:
        self._safe_mode.toggle(ModeChangeSource.LOCAL_TRAY)
        self.refresh()

    def _toggle_monitor(self) -> None:
        if self._monitor is not None:
            self._monitor.toggle()
            self.refresh()

    def _quit(self) -> None:
        if self._on_quit is not None:
            self._on_quit()
        self.stop()

    def _image(self) -> Any:
        """A dot: amber for SAFE MODE, teal when active.

        Drawn rather than shipped as an asset — one less binary in the tree, and
        the colour has to be computed from state anyway.
        """
        from PIL import Image, ImageDraw

        size = 64
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        colour = (
            (232, 152, 36, 255) if self._safe_mode.mode is AgentMode.SAFE else (32, 178, 170, 255)
        )
        draw.ellipse((6, 6, size - 6, size - 6), fill=colour)
        if self._safe_mode.mode is AgentMode.SAFE:
            # A bar through the dot, so the state is legible without colour.
            draw.rectangle((14, 28, size - 14, 36), fill=(28, 28, 28, 255))
        return image
