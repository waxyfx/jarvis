"""Activity sampling — including what it must never collect."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from atlas_agent import monitor as monitor_module
from atlas_agent.config import AgentSettings
from atlas_agent.monitor import NO_FOREGROUND, ActivityMonitor
from atlas_shared.protocol.messages import ActivityBatch, ActivitySample, SystemTelemetry


def settings(**overrides: object) -> AgentSettings:
    base: dict[str, object] = {
        "backend_url": "http://127.0.0.1:8000",
        "identity_path": Path("unused.json"),
        "monitor_interval_s": 1.0,
        "monitor_batch_size": 3,
        "monitor_idle_threshold_s": 60,
        "telemetry_interval_s": 10.0,
    }
    return AgentSettings(**(base | overrides))  # type: ignore[arg-type]


def build(
    process: str = "code.exe",
    idle: int = 0,
    *,
    interval_s: float | None = None,
    batch_size: int | None = None,
    **overrides: object,
) -> ActivityMonitor:
    return ActivityMonitor(
        settings(**overrides),
        process_sampler=lambda: process,
        idle_sampler=lambda: idle,
        interval_s=interval_s,
        batch_size=batch_size,
    )


class TestSampling:
    def test_sample_carries_only_metadata(self) -> None:
        sample = build(process="chrome.exe", idle=5).sample()

        assert sample.process_name == "chrome.exe"
        assert sample.idle_seconds == 5
        assert sample.is_idle is False

        # The wire model has no field that could carry content, and this is the
        # assertion that keeps it that way.
        assert set(ActivitySample.model_fields) == {
            "ts",
            "process_name",
            "is_idle",
            "idle_seconds",
        }

    def test_idle_threshold(self) -> None:
        assert build(idle=59).sample().is_idle is False
        assert build(idle=60).sample().is_idle is True
        assert build(idle=3600).sample().is_idle is True

    def test_process_name_is_bounded(self) -> None:
        sample = build(process="x" * 500).sample()
        assert len(sample.process_name) == 128

    def test_no_foreground_window_is_reported_honestly(self) -> None:
        # A plausible-looking name must never be invented for "nothing focused".
        assert build(process=NO_FOREGROUND).sample().process_name == NO_FOREGROUND

    def test_telemetry_is_plausible(self) -> None:
        telemetry = build().telemetry()
        assert isinstance(telemetry, SystemTelemetry)
        assert telemetry.ram_total_mb > 0
        assert 0 <= telemetry.ram_used_pct <= 100
        assert telemetry.uptime_s > 0


class TestPrivacyInvariants:
    """These fail loudly if collection ever widens by accident."""

    @staticmethod
    def _code_only() -> str:
        """Source with the module docstring removed.

        The docstring names the forbidden APIs in order to explain that they are
        not used; scanning it would make this test fail on its own explanation.
        """
        source = inspect.getsource(monitor_module)
        return source.split('"""', 2)[-1]

    def test_the_module_never_calls_window_text_apis(self) -> None:
        source = self._code_only()
        for forbidden in (
            "GetWindowText",
            "GetClipboardData",
            "SetWindowsHookEx",
            "GetKeyState",
            "GetAsyncKeyState",
            "keybd_event",
            "BitBlt",
        ):
            assert forbidden not in source, f"{forbidden} must never appear in the collector"

    def test_only_the_process_id_is_read_from_a_window(self) -> None:
        source = self._code_only()
        assert "GetWindowThreadProcessId" in source
        assert source.count("win32gui.") == 1  # GetForegroundWindow, nothing else


class TestPausing:
    def test_starts_paused_when_monitoring_is_disabled(self) -> None:
        assert build(monitor_enabled=False).paused is True

    def test_toggle_flips_and_reports(self) -> None:
        monitor = build()
        assert monitor.paused is False
        assert monitor.toggle() is True
        assert monitor.toggle() is False

    def test_pausing_drops_buffered_samples(self) -> None:
        monitor = build()
        monitor._pending.append(monitor.sample())
        monitor.pause()
        assert monitor._pending == []


class TestLoop:
    async def test_batches_are_sent_when_full(self) -> None:
        monitor = build(interval_s=0.01, batch_size=3)
        batches: list[ActivityBatch] = []
        stop = asyncio.Event()

        async def send_batch(batch: ActivityBatch) -> None:
            batches.append(batch)
            if len(batches) >= 2:
                stop.set()

        async def send_telemetry(_: SystemTelemetry) -> None:
            return None

        await asyncio.wait_for(
            monitor.run(send_batch=send_batch, send_telemetry=send_telemetry, stop=stop),
            timeout=10,
        )
        assert len(batches) >= 2
        assert all(len(batch.samples) == 3 for batch in batches[:2])

    async def test_nothing_is_sent_while_paused(self) -> None:
        monitor = build(interval_s=0.01)
        monitor.pause()
        batches: list[ActivityBatch] = []
        stop = asyncio.Event()

        async def send_batch(batch: ActivityBatch) -> None:
            batches.append(batch)

        async def send_telemetry(_: SystemTelemetry) -> None:
            return None

        task = asyncio.create_task(
            monitor.run(send_batch=send_batch, send_telemetry=send_telemetry, stop=stop)
        )
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

        assert batches == []

    async def test_a_send_failure_does_not_stop_sampling(self) -> None:
        monitor = build(interval_s=0.01, batch_size=1)
        attempts = 0
        stop = asyncio.Event()

        async def send_batch(_: ActivityBatch) -> None:
            nonlocal attempts
            attempts += 1
            if attempts >= 3:
                stop.set()
            raise ConnectionError("backend went away")

        async def send_telemetry(_: SystemTelemetry) -> None:
            return None

        await asyncio.wait_for(
            monitor.run(send_batch=send_batch, send_telemetry=send_telemetry, stop=stop),
            timeout=10,
        )
        # A dropped batch is acceptable; a dead collector is not.
        assert attempts >= 3

    async def test_buffered_samples_are_flushed_on_shutdown(self) -> None:
        monitor = build(interval_s=0.01, batch_size=1000)
        batches: list[ActivityBatch] = []
        stop = asyncio.Event()

        async def send_batch(batch: ActivityBatch) -> None:
            batches.append(batch)

        async def send_telemetry(_: SystemTelemetry) -> None:
            return None

        task = asyncio.create_task(
            monitor.run(send_batch=send_batch, send_telemetry=send_telemetry, stop=stop)
        )
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

        assert batches and batches[-1].samples


@pytest.mark.windows
def test_real_samplers_return_usable_values() -> None:
    name = monitor_module.foreground_process_name()
    idle = monitor_module.idle_seconds()
    assert isinstance(name, str) and name
    assert isinstance(idle, int) and idle >= 0
