"""Starting the agent when the user logs in — without administrator rights.

Implemented as a per-user ``Run`` entry under ``HKEY_CURRENT_USER``.

A scheduled task was tried first and rejected on evidence: every ``schtasks
/SC ONLOGON`` variant — with and without ``/RU``, at ``/RL LIMITED`` — is
refused with *Access is denied* for a non-elevated caller on Windows 11. Logon
triggers are an administrative operation. Requiring a UAC prompt to install the
agent would have been a worse trade than losing the scheduler's extra features,
so the Run key wins.

What this gives us, all of which the agent needs:

* no elevation, at install or at removal;
* the agent runs in the interactive session, which a Session 0 service could
  not do and which screen and input access will need from M6;
* it inherits the user's ordinary limited token, so the agent cannot inject
  input into elevated windows or reach the UAC desktop — a capability boundary,
  not an oversight;
* one value, in one place, removable by ``atlas-agent autostart uninstall`` or
  by hand in ``regedit``.

What it does not give us: automatic restart after a crash, and it does not run
when nobody is logged in. Neither matters for an agent that exists to serve the
person at the keyboard.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "ENTRY_NAME",
    "RUN_KEY",
    "AutostartError",
    "AutostartStatus",
    "agent_command",
    "install",
    "status",
    "uninstall",
]

ENTRY_NAME = "ATLAS Agent"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


class AutostartError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AutostartStatus:
    installed: bool
    detail: str


def agent_command() -> str:
    """The command line to run at logon.

    Prefers the installed console script; falls back to the interpreter, which
    keeps this working from a source checkout.
    """
    script = Path(sys.executable).parent / "atlas-agent.exe"
    if script.is_file():
        return f'"{script}" run'
    return f'"{sys.executable}" -m atlas_agent.cli run'


def install(*, command: str | None = None) -> AutostartStatus:
    """Register (or replace) the logon entry. Never prompts for elevation."""
    winreg = _winreg()
    value = command or agent_command()

    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, ENTRY_NAME, 0, winreg.REG_SZ, value)
    except OSError as exc:
        raise AutostartError(f"could not write the autostart entry: {exc}") from exc

    return AutostartStatus(installed=True, detail=value)


def uninstall() -> AutostartStatus:
    """Remove the logon entry. Succeeds quietly if it was never installed."""
    winreg = _winreg()

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, ENTRY_NAME)
    except FileNotFoundError:
        return AutostartStatus(installed=False, detail="was not installed")
    except OSError as exc:
        raise AutostartError(f"could not remove the autostart entry: {exc}") from exc

    return AutostartStatus(installed=False, detail="removed")


def status() -> AutostartStatus:
    if sys.platform != "win32":
        return AutostartStatus(installed=False, detail="autostart is Windows-only")

    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            value, _ = winreg.QueryValueEx(key, ENTRY_NAME)
    except FileNotFoundError:
        return AutostartStatus(installed=False, detail="not installed")
    except OSError as exc:
        return AutostartStatus(installed=False, detail=f"could not be read: {exc}")

    return AutostartStatus(installed=True, detail=str(value))


def _winreg() -> Any:
    """The ``winreg`` module, or a clear error off Windows.

    Imported lazily and typed as ``Any``: the module does not exist on other
    platforms, so a top-level import would break even reading this file there.
    """
    if sys.platform != "win32":
        raise AutostartError("autostart is only supported on Windows")
    import winreg

    return winreg
