"""Getting rendered speech to the speakers, and stopping it mid-word.

The counterpart to :mod:`atlas_voice.capture`, and thin for the same reason: the
sound card cannot be exercised in CI, so everything that could hold a bug is
kept on this side of it where a test can reach.

One requirement shapes the whole file. **Playback must stop immediately, not
politely.** Barge-in is the session cancelling the playback task while the
assistant is talking, and a player that finishes its sentence first has not been
interrupted — it has been queued behind. So the audio is fed through a callback
rather than a blocking write, and cancellation aborts the stream, discarding
whatever the driver has already buffered. The alternative, ``stream.stop()``,
drains that buffer and keeps talking for a further fraction of a second, which
is exactly the fraction the owner is trying to talk over.

Output is chosen separately from input, and that is not tidiness. A Bluetooth
headset switches to narrowband mono for as long as anything is recording, and
the assistant records continuously; keeping playback on the headset's A2DP
profile while the onboard microphone listens is only possible if the two devices
are configured apart.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np

from atlas_voice.providers import Utterance, VoiceEngineError

__all__ = ["Loudspeaker", "OutputInfo", "list_output_devices", "silence"]


@dataclass(frozen=True)
class OutputInfo:
    index: int
    name: str
    channels: int
    default_rate: float
    is_default: bool = False


def _sounddevice() -> Any:
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        raise VoiceEngineError(
            "sounddevice is required to play audio; install atlas-voice[audio]"
        ) from exc
    return sounddevice


def list_output_devices() -> list[OutputInfo]:
    """Every speaker Windows will admit to, for the settings UI."""
    sounddevice = _sounddevice()
    try:
        default_output = sounddevice.default.device[1]
    except (TypeError, IndexError):
        default_output = None

    devices: list[OutputInfo] = []
    for index, raw in enumerate(sounddevice.query_devices()):
        if int(raw.get("max_output_channels", 0)) < 1:
            continue
        devices.append(
            OutputInfo(
                index=index,
                name=str(raw.get("name", f"device {index}")),
                channels=int(raw["max_output_channels"]),
                default_rate=float(raw.get("default_samplerate", 0.0)),
                is_default=index == default_output,
            )
        )
    return devices


class _Cursor:
    """Hands out successive slices of one utterance.

    Separated from the stream so the part with arithmetic in it can be tested
    without a sound card. A short read is padded rather than truncated: the
    callback must fill the whole output block or the driver plays whatever was
    left in it, which is audible as a click.
    """

    def __init__(self, samples: np.ndarray) -> None:
        self._samples = samples
        self.position = 0

    @property
    def finished(self) -> bool:
        return self.position >= len(self._samples)

    def take(self, frames: int) -> tuple[np.ndarray, bool]:
        """The next ``frames`` samples, and whether that was the last of it."""
        chunk = self._samples[self.position : self.position + frames]
        self.position += len(chunk)
        if len(chunk) < frames:
            chunk = np.concatenate([chunk, np.zeros(frames - len(chunk), dtype=np.float32)])
            return chunk.astype(np.float32, copy=False), True
        return chunk.astype(np.float32, copy=False), self.finished


class Loudspeaker:
    """Plays utterances on a chosen output. One at a time, cancellable."""

    def __init__(self, *, device: int | None = None, block_ms: int = 32) -> None:
        self._device = device
        self._block_ms = block_ms
        self._stream: Any = None
        #: Only one utterance may hold the device. Two overlapping calls used to
        #: segfault the process — PortAudio was left with a stream nobody had a
        #: reference to any more, because the second call overwrote the first's.
        #: Reachable in ordinary use: the acknowledgement is still playing when
        #: a fast turn's reply arrives.
        self._turn = asyncio.Lock()
        #: Counted rather than ignored: a device that cannot keep up produces
        #: gaps in speech, and silence is the hardest fault to notice.
        self.underruns = 0

    async def play(self, utterance: Utterance) -> None:
        """Play to the end, or until cancelled.

        Cancellation is the normal path, not an error: it is what barge-in does.

        A later utterance takes the device from an earlier one rather than
        queueing behind it. That is the right way round for speech — the newer
        thing is the one worth hearing — and it is also the only safe way: two
        streams open at once on this object crashed the process.
        """
        if len(utterance.samples) == 0:
            return

        # Take the device before waiting for the lock, so the utterance being
        # superseded stops now rather than when it happens to notice.
        self._abort()
        async with self._turn:
            await self._play_one(utterance)

    async def _play_one(self, utterance: Utterance) -> None:
        sounddevice = _sounddevice()
        cursor = _Cursor(np.asarray(utterance.samples, dtype=np.float32))
        drained = threading.Event()

        def callback(outdata: np.ndarray, frames: int, _time: Any, status: Any) -> None:
            if status:
                self.underruns += 1
            chunk, last = cursor.take(frames)
            outdata[:, 0] = chunk
            if last:
                raise sounddevice.CallbackStop

        try:
            stream = sounddevice.OutputStream(
                samplerate=utterance.sample_rate,
                blocksize=max(1, int(utterance.sample_rate * self._block_ms / 1000)),
                device=self._device,
                channels=1,
                dtype="float32",
                callback=callback,
                finished_callback=drained.set,
            )
        except Exception as exc:
            raise VoiceEngineError(f"could not open the speakers: {type(exc).__name__}") from exc

        self._stream = stream
        stream.start()
        try:
            while not drained.is_set():
                # Polled rather than awaited on the event, because the event is
                # set from the sound library's thread and asyncio primitives are
                # not safe to touch from there.
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self._abort()
            raise
        finally:
            self._close(stream)

    def stop(self) -> None:
        """Cut playback from outside the task that started it."""
        self._abort()

    def _abort(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            # abort, not stop: discards the driver's buffer instead of draining
            # it. The difference is whether the owner has to talk over a tail.
            stream.abort(ignore_errors=True)
        finally:
            stream.close(ignore_errors=True)

    def _close(self, stream: Any) -> None:
        """Release one stream, and only clear the attribute if it is still ours.

        Whoever holds the device may have changed while this utterance was
        playing. Clearing unconditionally would drop the reference to somebody
        else's live stream, which is the shape of the crash this replaced.
        """
        if self._stream is stream:
            self._stream = None
        stream.close(ignore_errors=True)


def silence(seconds: float, sample_rate: int) -> np.ndarray:
    """A gap, for spacing utterances in a scripted playback."""
    return np.zeros(int(seconds * sample_rate), dtype=np.float32)
