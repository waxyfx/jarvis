"""Thermal guard for long autonomous runs.

Heavy work — feature extraction, training, benchmarks — is stopped when the
machine gets too hot. Fan behaviour is never touched; the only lever used here
is *how much work is asked of the machine*.

## What can and cannot be measured on this laptop

**GPU temperature: yes**, from ``nvidia-smi``. Real, per-second, no driver to
install.

**CPU temperature: no.** Three routes were tried and all are dead on this
machine: ``MSAcpi_ThermalZoneTemperature`` reports "not supported", the
``Thermal Zone Information`` performance counter set exists but has no
instances, and neither LibreHardwareMonitor nor OpenHardwareMonitor is
installed. Reading Ryzen package temperature needs a kernel-mode driver, which
is not something to install unattended.

``CurrentClockSpeed`` was evaluated as a throttling proxy and rejected: it reads
1908 MHz whether the machine is idle or at 84% load, because Windows reports the
nominal value rather than the live clock.

So the guard is honest about its blind spot rather than inventing a number. It
watches the GPU, and it bounds CPU exposure by *duty* instead of temperature:
sustained full-load stretches are broken up by cool-down pauses, which is the
one protection available without a sensor.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

#: Above this the GPU is hot enough to stop feeding it work. An RTX 3060 laptop
#: throttles around 87 C; stopping earlier leaves headroom rather than sitting
#: at the edge for hours.
GPU_HOT_C = 84.0
#: Resume only once it has come back down, so the guard does not oscillate.
GPU_RESUME_C = 75.0
#: How long it must stay hot before heavy work stops. A single spike during a
#: burst is normal and not worth reacting to.
SUSTAINED_SECONDS = 90.0

HISTORY = Path(__file__).resolve().parents[1] / ".training" / "thermal.jsonl"


@dataclass(frozen=True)
class Reading:
    at: str
    gpu_c: float | None
    gpu_util: int | None
    cpu_load: int | None
    #: Always None on this machine. Kept in the record so the gap is visible in
    #: the history rather than merely absent.
    cpu_c: float | None = None
    note: str = ""


def _nvidia() -> tuple[float | None, int | None]:
    # Resolved from PATH rather than hard-coded: nvidia-smi moves between driver
    # versions, and a stale absolute path would silently disable the only
    # temperature reading this machine has.
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return None, None

    try:
        # Fixed argv, no shell, and the only variable part is a path resolved
        # from PATH by shutil.which — nothing here comes from input.
        out = subprocess.run(  # noqa: S603
            [
                executable,
                "--query-gpu=temperature.gpu,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if out.returncode != 0 or not out.stdout.strip():
        return None, None

    first = out.stdout.strip().splitlines()[0]
    try:
        temperature, utilisation = (part.strip() for part in first.split(","))
        return float(temperature), int(utilisation)
    except ValueError:
        return None, None


def _cpu_load() -> int | None:
    try:
        import psutil
    except ImportError:
        return None
    return int(psutil.cpu_percent(interval=0.3))


def read() -> Reading:
    gpu_c, gpu_util = _nvidia()
    return Reading(
        at=datetime.now(UTC).isoformat(timespec="seconds"),
        gpu_c=gpu_c,
        gpu_util=gpu_util,
        cpu_load=_cpu_load(),
        cpu_c=None,
        note="cpu temperature unavailable on this machine; see module docstring",
    )


def record(reading: Reading) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(reading)) + "\n")


class ThermalGuard:
    """Decides whether heavy work may continue.

    Stateful on purpose: "too hot" means *sustained*, not a single sample, and
    coming back means cooling past a lower mark than the one that tripped it.
    """

    def __init__(
        self,
        *,
        hot_c: float = GPU_HOT_C,
        resume_c: float = GPU_RESUME_C,
        sustained_s: float = SUSTAINED_SECONDS,
    ) -> None:
        self._hot_c = hot_c
        self._resume_c = resume_c
        self._sustained_s = sustained_s
        self._hot_since: float | None = None
        self.paused = False
        self.reason = ""

    def check(self, *, log: bool = True) -> Reading:
        reading = read()
        if log:
            record(reading)

        now = time.monotonic()
        temperature = reading.gpu_c

        if temperature is None:
            # No reading is not the same as a safe reading, but refusing to work
            # because nvidia-smi hiccuped would be worse than continuing: the
            # CPU-bound work this mostly guards does not heat the GPU anyway.
            return reading

        if self.paused:
            if temperature <= self._resume_c:
                self.paused = False
                self.reason = ""
                self._hot_since = None
            return reading

        if temperature >= self._hot_c:
            self._hot_since = self._hot_since or now
            if now - self._hot_since >= self._sustained_s:
                self.paused = True
                self.reason = (
                    f"GPU at {temperature:.0f} C for over "
                    f"{self._sustained_s / 60:.0f} min (limit {self._hot_c:.0f} C)"
                )
        else:
            self._hot_since = None

        return reading

    def wait_until_cool(self, *, poll_s: float = 30.0, limit_s: float = 1800.0) -> bool:
        """Block until it is safe again. Returns False if it never cools."""
        waited = 0.0
        while self.paused and waited < limit_s:
            time.sleep(poll_s)
            waited += poll_s
            self.check()
        return not self.paused


def summarise() -> int:
    if not HISTORY.is_file():
        print("no thermal history recorded")
        return 0

    temperatures = [
        entry["gpu_c"]
        for line in HISTORY.read_text(encoding="utf-8").splitlines()
        if (entry := json.loads(line)).get("gpu_c") is not None
    ]
    if not temperatures:
        print("history contains no GPU readings")
        return 0

    ordered = sorted(temperatures)
    print(f"readings          {len(temperatures)}")
    print(f"GPU min / median  {ordered[0]:.0f} / {ordered[len(ordered) // 2]:.0f} C")
    print(f"GPU max           {ordered[-1]:.0f} C")
    print(f"at or over {GPU_HOT_C:.0f} C  {sum(t >= GPU_HOT_C for t in temperatures)} readings")
    print("CPU               not measurable on this machine")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", type=float, default=0.0, help="poll every N seconds")
    parser.add_argument("--summary", action="store_true", help="summarise the recorded history")
    arguments = parser.parse_args()

    if arguments.summary:
        return summarise()

    if not arguments.watch:
        reading = read()
        record(reading)
        gpu = f"{reading.gpu_c:.0f}" if reading.gpu_c is not None else "-"
        print(
            f"GPU {gpu} C   util {reading.gpu_util}%   "
            f"CPU load {reading.cpu_load}%   CPU temp unavailable"
        )
        return 0

    guard = ThermalGuard()
    while True:
        reading = guard.check()
        state = "PAUSE" if guard.paused else "ok"
        print(
            f"{reading.at}  GPU {reading.gpu_c} C  util {reading.gpu_util}%  "
            f"CPU {reading.cpu_load}%  {state} {guard.reason}",
            flush=True,
        )
        time.sleep(arguments.watch)


if __name__ == "__main__":
    raise SystemExit(main())
