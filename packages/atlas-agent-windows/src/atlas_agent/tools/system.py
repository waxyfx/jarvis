"""System metrics. Read-only, LOW risk."""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Any

import psutil

from atlas_agent.tools.base import ExecutionContext, register_executor
from atlas_shared.tools.catalog import SystemMetricsArgs

__all__ = ["read_metrics"]


@register_executor("system.metrics")
def read_metrics(args: SystemMetricsArgs, context: ExecutionContext) -> dict[str, Any]:
    """CPU, memory, disks, uptime and GPU temperature where available."""
    del args, context  # no inputs, no filesystem access

    memory = psutil.virtual_memory()
    boot_time = psutil.boot_time()

    return {
        # interval=None returns the average since the previous call rather than
        # blocking for a sampling window.
        "cpu_pct": round(psutil.cpu_percent(interval=None), 1),
        "cpu_count": psutil.cpu_count(logical=True),
        "ram_used_pct": round(memory.percent, 1),
        "ram_total_mb": memory.total // (1024 * 1024),
        "ram_available_mb": memory.available // (1024 * 1024),
        "disks": _disks(),
        "uptime_s": int(time.time() - boot_time),
        "gpu_temp_c": _gpu_temperature(),
        "process_count": len(psutil.pids()),
    }


def _disks() -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    for partition in psutil.disk_partitions(all=False):
        try:
            usage = shutil.disk_usage(partition.mountpoint)
        except OSError:
            # Empty card readers and disconnected network drives raise here.
            continue
        disks.append(
            {
                "mount": partition.mountpoint,
                "total_gb": round(usage.total / 1024**3, 1),
                "free_gb": round(usage.free / 1024**3, 1),
                "used_pct": round(100 * usage.used / usage.total, 1) if usage.total else 0.0,
            }
        )
    return disks


def _gpu_temperature() -> float | None:
    """GPU temperature via nvidia-smi, or ``None``.

    CPU temperature is deliberately not attempted: on Windows it needs a
    kernel-level driver and administrator rights, and reporting a wrong number
    would be worse than reporting none. See PHASE-0 §2.
    """
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return None

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, resolved absolute path
            [executable, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if completed.returncode != 0:
        return None
    first_line = completed.stdout.strip().splitlines()
    try:
        return float(first_line[0].strip())
    except (IndexError, ValueError):
        return None
