"""Activity sampling — metadata only.

**What this collects:** the name of the executable that owns the foreground
window, whether the user is idle, how long they have been idle, and system
counters (CPU, memory, disk, uptime).

**What this deliberately does not collect, and has no code path for:**

* window titles — a title routinely contains a document name, a URL, an email
  subject or a customer's name, which is content, not metadata;
* keystrokes;
* clipboard contents;
* screen contents;
* anything typed into, or displayed by, any application.

The only Windows call made against a window is
``GetWindowThreadProcessId`` — which returns a process id and nothing else.
``GetWindowText`` is never called. The database schema has no column that could
hold any of the above either (see migration 0002), so adding collection later
would require a visible migration rather than a quiet change here.

Sampling is pausable from the tray, and the pause is honoured immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import psutil

from atlas_agent.config import AgentSettings
from atlas_agent.logging import get_logger
from atlas_shared.protocol.messages import (
    ActivityBatch,
    ActivitySample,
    DiskUsage,
    SystemTelemetry,
)

__all__ = ["ActivityMonitor", "foreground_process_name", "idle_seconds"]

log = get_logger(__name__)

SendBatch = Callable[[ActivityBatch], Awaitable[None]]
SendTelemetry = Callable[[SystemTelemetry], Awaitable[None]]

#: Reported when no window has focus — the lock screen, or a desktop with
#: nothing open. A real name is never invented for it.
NO_FOREGROUND = "(none)"


def foreground_process_name() -> str:
    """Executable name of the foreground window's process.

    Returns :data:`NO_FOREGROUND` when nothing has focus or the owner cannot be
    read (an elevated process, for instance — which a non-elevated agent cannot
    and should not inspect).
    """
    if sys.platform != "win32":
        return NO_FOREGROUND

    try:
        import win32gui
        import win32process
    except ImportError:  # pragma: no cover - Windows-only
        return NO_FOREGROUND

    try:
        handle = win32gui.GetForegroundWindow()
        if not handle:
            return NO_FOREGROUND
        # Note: the process id, and only the process id. The window's *text* is
        # never requested.
        _, process_id = win32process.GetWindowThreadProcessId(handle)
        if not process_id:
            return NO_FOREGROUND
        return str(psutil.Process(process_id).name())
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, Exception):
        return NO_FOREGROUND


def idle_seconds() -> int:
    """Seconds since the last keyboard or mouse input.

    Reads only *when* input last happened. Which key, which button and where the
    pointer went are not available from this API and are not sought elsewhere.
    """
    if sys.platform != "win32":
        return 0

    try:
        import win32api
    except ImportError:  # pragma: no cover - Windows-only
        return 0

    try:
        last_input = int(win32api.GetLastInputInfo())
        ticks = int(win32api.GetTickCount())
    except Exception:  # pywin32 raises bare errors
        return 0
    return max(0, (ticks - last_input) // 1000)


class ActivityMonitor:
    def __init__(
        self,
        settings: AgentSettings,
        *,
        process_sampler: Callable[[], str] = foreground_process_name,
        idle_sampler: Callable[[], int] = idle_seconds,
        interval_s: float | None = None,
        batch_size: int | None = None,
    ) -> None:
        self._settings = settings
        self._process_sampler = process_sampler
        self._idle_sampler = idle_sampler
        # Overridable so tests can run the loop fast without weakening the
        # bounds that keep a real deployment from sampling itself to death.
        self._interval_s = interval_s or settings.monitor_interval_s
        self._batch_size = batch_size or settings.monitor_batch_size
        self._paused = not settings.monitor_enabled
        self._pending: list[ActivitySample] = []

    # ------------------------------------------------------------------ state

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        """Stop sampling. Anything already buffered is dropped, not sent."""
        self._paused = True
        self._pending.clear()

    def resume(self) -> None:
        self._paused = False

    def toggle(self) -> bool:
        """Flip and return the new paused state. Used by the tray."""
        if self._paused:
            self.resume()
        else:
            self.pause()
        return self._paused

    # --------------------------------------------------------------- sampling

    def sample(self) -> ActivitySample:
        idle = self._idle_sampler()
        return ActivitySample(
            ts=datetime.now(UTC),
            process_name=self._process_sampler()[:128],
            is_idle=idle >= self._settings.monitor_idle_threshold_s,
            idle_seconds=idle,
        )

    def telemetry(self) -> SystemTelemetry:
        memory = psutil.virtual_memory()
        disks: list[DiskUsage] = []
        for partition in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(partition.mountpoint)
            except OSError:
                continue
            disks.append(
                DiskUsage(
                    mount=partition.mountpoint,
                    total_gb=round(usage.total / 1024**3, 1),
                    free_gb=round(usage.free / 1024**3, 1),
                    used_pct=round(usage.percent, 1),
                )
            )

        return SystemTelemetry(
            cpu_pct=round(psutil.cpu_percent(interval=None), 1),
            ram_used_pct=round(memory.percent, 1),
            ram_total_mb=memory.total // (1024 * 1024),
            disks=tuple(disks),
            uptime_s=int(time.time() - psutil.boot_time()),
            gpu_temp_c=None,
        )

    # ------------------------------------------------------------------- loop

    async def run(
        self,
        *,
        send_batch: SendBatch,
        send_telemetry: SendTelemetry,
        stop: asyncio.Event,
    ) -> None:
        """Sample until ``stop`` is set, flushing in batches.

        Sending failures are logged and the batch is dropped rather than
        retried: activity metadata is not worth queueing across a reconnect,
        and an unbounded buffer would be a memory leak on a long outage.
        """
        telemetry_due = 0.0

        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
            if stop.is_set():
                break
            if self._paused:
                continue

            self._pending.append(self.sample())

            if len(self._pending) >= self._batch_size:
                batch = ActivityBatch(samples=tuple(self._pending))
                self._pending.clear()
                try:
                    await send_batch(batch)
                except Exception as exc:
                    log.warning("activity_batch_dropped", error=str(exc))

            telemetry_due += self._interval_s
            if telemetry_due >= self._settings.telemetry_interval_s:
                telemetry_due = 0.0
                try:
                    await send_telemetry(self.telemetry())
                except Exception as exc:
                    log.warning("telemetry_dropped", error=str(exc))

        # Flush whatever is buffered so a clean shutdown does not lose it.
        if self._pending and not self._paused:
            with contextlib.suppress(Exception):
                await send_batch(ActivityBatch(samples=tuple(self._pending)))
            self._pending.clear()
