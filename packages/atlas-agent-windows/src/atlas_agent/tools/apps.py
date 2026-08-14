"""Application tools: list, launch, close.

Launching never goes through a shell. Arguments are passed as an argv list to
``CreateProcess``, so no part of a tool argument can be reinterpreted as a
command — there is no string for an injected `&&` or `;` to live in.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import psutil

from atlas_agent.tools.base import ExecutionContext, ToolExecutionError, register_executor
from atlas_shared.tools.catalog import AppCloseArgs, AppLaunchArgs, AppListArgs

__all__ = ["close_app", "launch_app", "list_apps"]

#: Friendly names an operator actually says, mapped to what Windows calls them.
_ALIASES: dict[str, str] = {
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "edge": "msedge.exe",
    "firefox": "firefox.exe",
    "vs code": "code.exe",
    "vscode": "code.exe",
    "code": "code.exe",
    "notepad": "notepad.exe",
    "explorer": "explorer.exe",
    "calculator": "calc.exe",
    "terminal": "wt.exe",
    "powershell": "powershell.exe",
    "spotify": "spotify.exe",
    "telegram": "telegram.exe",
    "discord": "discord.exe",
    "steam": "steam.exe",
}

_APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"


@register_executor("app.list")
def list_apps(args: AppListArgs, context: ExecutionContext) -> dict[str, Any]:
    """Running processes, and optionally installed Store applications."""
    del context

    processes: list[dict[str, Any]] = []
    for process in psutil.process_iter(["pid", "name", "memory_info", "create_time"]):
        try:
            info = process.info
            memory = info.get("memory_info")
            processes.append(
                {
                    "pid": info["pid"],
                    "name": info["name"],
                    "memory_mb": round(memory.rss / (1024 * 1024), 1) if memory else None,
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            # Processes disappear mid-iteration, and some are not ours to read.
            continue

    processes.sort(key=lambda item: item["memory_mb"] or 0, reverse=True)

    result: dict[str, Any] = {
        "processes": processes[:200],
        "process_count": len(processes),
    }
    if args.include_store_apps:
        result["installed"] = _installed_from_app_paths()
    return result


@register_executor("app.launch")
def launch_app(args: AppLaunchArgs, context: ExecutionContext) -> dict[str, Any]:
    """Start an application, by resolved path or by friendly name."""
    del context

    executable = (
        _validate_explicit_path(args.executable_path)
        if args.executable_path
        else _resolve_name(args.name)
    )

    try:
        # shell=False and an argv list: nothing here can be reinterpreted as a
        # command line. DETACHED_PROCESS so the app outlives the agent.
        detached = getattr(subprocess, "DETACHED_PROCESS", 0)
        creation_flags = detached if sys.platform == "win32" else 0
        process = subprocess.Popen(  # noqa: S603 - argv list, shell=False, resolved path
            [str(executable), *args.arguments],
            shell=False,
            creationflags=creation_flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ToolExecutionError("launch_failed", f"could not start {executable}: {exc}") from exc

    return {"pid": process.pid, "executable": str(executable), "name": args.name}


@register_executor("app.close")
def close_app(args: AppCloseArgs, context: ExecutionContext) -> dict[str, Any]:
    """Close an application.

    Without ``force`` this asks windows to close, which lets the application
    prompt about unsaved work. With ``force`` the process is terminated and
    anything unsaved is lost — which is why forcing escalates to HIGH risk.
    """
    del context

    if args.pid is None and not args.name:
        raise ToolExecutionError("args_invalid", "either name or pid is required")

    targets = _find_processes(name=args.name, pid=args.pid)
    if not targets:
        raise ToolExecutionError("not_found", "no matching process is running")

    closed: list[dict[str, Any]] = []
    for process in targets:
        try:
            if args.force:
                process.kill()
                method = "terminated"
            else:
                method = "close_requested" if _request_close(process.pid) else "no_window"
            closed.append({"pid": process.pid, "name": process.name(), "method": method})
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            closed.append({"pid": process.pid, "error": str(exc)})

    return {"closed": closed, "count": len(closed)}


# ------------------------------------------------------------------ helpers


def _validate_explicit_path(raw: str) -> Path:
    candidate = Path(raw)
    if not candidate.is_file():
        raise ToolExecutionError("not_found", f"no executable at {raw}")
    return candidate


def _resolve_name(name: str) -> Path:
    """Turn a spoken name into an executable, or fail loudly.

    Order: alias table, then PATH, then the Windows "App Paths" registry, which
    is how the Run dialog resolves names like ``chrome``.
    """
    normalised = name.strip().lower()
    candidate = _ALIASES.get(normalised, normalised)
    if not candidate.endswith(".exe"):
        candidate = f"{candidate}.exe"

    found = shutil.which(candidate)
    if found:
        return Path(found)

    registered = _lookup_app_path(candidate)
    if registered is not None:
        return registered

    raise ToolExecutionError(
        "not_found",
        f"could not resolve '{name}' to an installed application",
    )


def _lookup_app_path(executable: str) -> Path | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows-only
        return None

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"{_APP_PATHS_KEY}\{executable}") as key:
                value, _ = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        path = Path(str(value).strip('"'))
        if path.is_file():
            return path
    return None


def _installed_from_app_paths() -> list[dict[str, str]]:
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows-only
        return []

    seen: dict[str, str] = {}
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, _APP_PATHS_KEY) as key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        name = winreg.EnumKey(key, index)
                        with winreg.OpenKey(key, name) as sub:
                            value, _ = winreg.QueryValueEx(sub, "")
                        seen.setdefault(name, str(value).strip('"'))
                    except OSError:
                        continue
        except OSError:
            continue

    return [{"name": name, "path": path} for name, path in sorted(seen.items())]


def _find_processes(*, name: str | None, pid: int | None) -> list[psutil.Process]:
    if pid is not None:
        try:
            return [psutil.Process(pid)]
        except psutil.NoSuchProcess:
            return []

    assert name is not None
    wanted = _ALIASES.get(name.strip().lower(), name.strip().lower())
    if not wanted.endswith(".exe"):
        wanted = f"{wanted}.exe"

    matches: list[psutil.Process] = []
    current = os.getpid()
    for process in psutil.process_iter(["pid", "name"]):
        try:
            if process.info["name"] and process.info["name"].lower() == wanted:
                # Never close ourselves: that would look like a crash rather
                # than a decision, and would drop the connection mid-command.
                if process.info["pid"] != current:
                    matches.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return matches


def _request_close(pid: int) -> bool:
    """Ask a process's top-level windows to close. Returns False if it has none."""
    if sys.platform != "win32":
        return False
    try:
        import win32con
        import win32gui
        import win32process
    except ImportError:  # pragma: no cover - Windows-only
        return False

    posted = False

    def visit(handle: int, _extra: object) -> bool:
        nonlocal posted
        try:
            _, window_pid = win32process.GetWindowThreadProcessId(handle)
        except Exception:  # pywin32 raises bare errors here
            return True
        if window_pid == pid and win32gui.IsWindowVisible(handle):
            win32gui.PostMessage(handle, win32con.WM_CLOSE, 0, 0)
            posted = True
        return True

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:  # EnumWindows raises when a callback fails
        return posted
    return posted
