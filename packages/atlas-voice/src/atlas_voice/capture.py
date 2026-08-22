"""Getting audio off the microphone, behind something testable.

The device is the one part of the voice engine that cannot be exercised in CI,
so it is kept behind a protocol and made as thin as possible: everything that
could hold a bug — levels, framing, clipping, timing — lives on the other side
of it, where a test can reach.

Two details matter on this machine specifically.

**Bluetooth headsets change their own audio when the microphone opens.** A
G435, like any HFP device, drops the output to narrowband mono for as long as it
is recording. Since the assistant listens continuously, that is always. Input
and output devices are therefore chosen separately, so the headset can stay on
A2DP for playback while the onboard microphone does the listening.

**The device rate is whatever the device wants.** Windows commonly offers 44.1
or 48 kHz and the pipeline runs at 16 kHz, so capture resamples rather than
asking the driver to do it — some drivers oblige by silently decimating, which
aliases speech into the band the recogniser cares about.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import numpy as np

from atlas_voice.audio import FRAME_SAMPLES, SAMPLE_RATE, Frame, resample
from atlas_voice.providers import VoiceEngineError

__all__ = ["AudioDevice", "DeviceInfo", "Microphone", "list_input_devices"]


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    channels: int
    default_rate: float
    is_default: bool = False


@runtime_checkable
class AudioDevice(Protocol):
    """A source of pipeline frames."""

    def frames(self) -> Iterator[Frame]:
        """Yield 32 ms frames until :meth:`stop` is called."""
        ...

    def stop(self) -> None: ...


def _sounddevice() -> Any:
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        # OSError too: PortAudio is a native library and its absence surfaces
        # as a load failure rather than a missing module.
        raise VoiceEngineError(
            "sounddevice is required to use the microphone; install atlas-voice[audio]"
        ) from exc
    return sounddevice


def list_input_devices() -> list[DeviceInfo]:
    """Every microphone Windows will admit to, for the settings UI."""
    sounddevice = _sounddevice()
    try:
        default_input = sounddevice.default.device[0]
    except (TypeError, IndexError):
        default_input = None

    devices: list[DeviceInfo] = []
    for index, raw in enumerate(sounddevice.query_devices()):
        if int(raw.get("max_input_channels", 0)) < 1:
            continue
        devices.append(
            DeviceInfo(
                index=index,
                name=str(raw.get("name", f"device {index}")),
                channels=int(raw["max_input_channels"]),
                default_rate=float(raw.get("default_samplerate", 0.0)),
                is_default=index == default_input,
            )
        )
    return devices


class Microphone:
    """Reads the default (or chosen) input and yields pipeline frames.

    Capture runs on the sound library's own thread and hands blocks to a queue;
    the consumer sees a plain iterator. Dropping blocks when the queue is full
    is deliberate: a consumer that has fallen behind wants the *latest* audio,
    and an unbounded queue would trade a stutter for growing latency that never
    recovers.
    """

    def __init__(
        self,
        *,
        device: int | None = None,
        block_ms: int = 32,
        queue_frames: int = 64,
    ) -> None:
        self._device = device
        self._block_ms = block_ms
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=queue_frames)
        self._stop = threading.Event()
        self._stream: Any = None
        self._pending = np.zeros(0, dtype=np.float32)
        self._elapsed = 0.0
        #: Counted rather than ignored, so a stuttering device is visible.
        self.dropped_blocks = 0

    def _callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        # Mono: a stereo microphone gives two nearly identical channels and the
        # models want one.
        block = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()
        try:
            self._queue.put_nowait(block.astype(np.float32))
        except queue.Full:
            self.dropped_blocks += 1

    def frames(self) -> Iterator[Frame]:
        sounddevice = _sounddevice()
        device_rate = int(
            sounddevice.query_devices(self._device, "input")["default_samplerate"] or SAMPLE_RATE
        )
        blocksize = max(1, int(device_rate * self._block_ms / 1000))

        try:
            self._stream = sounddevice.InputStream(
                samplerate=device_rate,
                blocksize=blocksize,
                device=self._device,
                channels=1,
                dtype="float32",
                callback=self._callback,
            )
        except Exception as exc:
            raise VoiceEngineError(f"could not open the microphone: {type(exc).__name__}") from exc

        with self._stream:
            while not self._stop.is_set():
                try:
                    block = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                if device_rate != SAMPLE_RATE:
                    block = resample(block, from_rate=device_rate, to_rate=SAMPLE_RATE)
                self._pending = np.concatenate([self._pending, block])

                while len(self._pending) >= FRAME_SAMPLES:
                    chunk = self._pending[:FRAME_SAMPLES]
                    self._pending = self._pending[FRAME_SAMPLES:]
                    yield Frame(samples=chunk, started_at=self._elapsed)
                    self._elapsed += FRAME_SAMPLES / SAMPLE_RATE

    def stop(self) -> None:
        self._stop.set()

    def record(self, seconds: float) -> np.ndarray:
        """Capture a fixed stretch. Used by enrollment, which is turn-based."""
        wanted = int(seconds * SAMPLE_RATE)
        collected: list[np.ndarray] = []
        gathered = 0
        for frame in self.frames():
            collected.append(frame.samples)
            gathered += len(frame.samples)
            if gathered >= wanted:
                break
        self.stop()
        return np.concatenate(collected)[:wanted] if collected else np.zeros(0, dtype=np.float32)
