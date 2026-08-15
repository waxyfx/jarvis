"""Audio primitives shared by every stage of the voice engine.

One format, fixed everywhere: **16 kHz, mono, float32 in [-1, 1]**. Every model
in the chosen stack — Silero, openWakeWord, Whisper, ECAPA — wants exactly that,
and a pipeline that converts between rates at each hop spends its time
resampling and its bugs on off-by-one frame boundaries.

Nothing here imports a sound library. A :class:`Frame` is a numpy array with a
timestamp, so the whole pipeline can be driven from a WAV file in a test as
easily as from a microphone, and CI needs no audio device at all.
"""

from __future__ import annotations

import wave
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np

__all__ = [
    "FRAME_MS",
    "FRAME_SAMPLES",
    "SAMPLE_RATE",
    "Frame",
    "RingBuffer",
    "frames_from_array",
    "read_wav",
    "write_wav",
]

#: The one true format.
SAMPLE_RATE = 16_000
#: Silero VAD wants 512-sample windows at 16 kHz; openWakeWord wants 1280 (80 ms).
#: 32 ms divides into both awkwardly, so the pipeline frame is 32 ms and the
#: engines buffer internally to whatever they need. Keeping *one* frame size in
#: the transport avoids a second timeline to reason about.
FRAME_MS = 32
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 512


@dataclass(frozen=True, slots=True)
class Frame:
    """A fixed-length window of mono audio.

    ``started_at`` is seconds since the stream opened, not wall clock. Latency
    assertions need a monotonic origin, and wall clock drifts.
    """

    samples: np.ndarray
    started_at: float

    def __post_init__(self) -> None:
        if self.samples.dtype != np.float32:
            raise ValueError(f"frames are float32, got {self.samples.dtype}")
        if self.samples.ndim != 1:
            raise ValueError(f"frames are mono, got shape {self.samples.shape}")

    @property
    def duration_s(self) -> float:
        return len(self.samples) / SAMPLE_RATE

    @property
    def ends_at(self) -> float:
        return self.started_at + self.duration_s

    @property
    def peak(self) -> float:
        """Loudest absolute sample. Zero for digital silence."""
        return float(np.abs(self.samples).max(initial=0.0))

    @property
    def rms(self) -> float:
        return float(np.sqrt(np.mean(np.square(self.samples)))) if len(self.samples) else 0.0

    @property
    def is_clipped(self) -> bool:
        """Whether the microphone was driven past full scale.

        Enrollment rejects clipped takes: a clipped waveform has had its peaks
        flattened, and the embedding computed from it describes the clipping as
        much as the speaker.
        """
        return bool(np.count_nonzero(np.abs(self.samples) >= 0.999) >= 3)


class RingBuffer:
    """The last N seconds of audio, kept so a wake word can be reconsidered.

    The wake word is recognised *after* it has been spoken, and the two things
    that happen next — verifying the speaker, and transcribing a command that
    may have followed immediately — both need audio from before the detection.
    Without a pre-roll, "Atlas, закрой Notepad" said in one breath loses the
    command.
    """

    def __init__(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError("a ring buffer needs a positive duration")
        self._max_frames = max(1, int(seconds * 1000 / FRAME_MS))
        self._frames: deque[Frame] = deque(maxlen=self._max_frames)

    def push(self, frame: Frame) -> None:
        self._frames.append(frame)

    def clear(self) -> None:
        self._frames.clear()

    def __len__(self) -> int:
        return len(self._frames)

    @property
    def seconds_held(self) -> float:
        return len(self._frames) * FRAME_MS / 1000

    def tail(self, seconds: float) -> np.ndarray:
        """The most recent ``seconds`` of audio as one array.

        Returns whatever it has when asked for more than it holds — a short
        buffer is a normal condition just after the stream opens, not an error.
        """
        wanted = max(1, int(seconds * 1000 / FRAME_MS))
        frames = list(self._frames)[-wanted:]
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate([frame.samples for frame in frames])


def frames_from_array(
    samples: np.ndarray, *, start_at: float = 0.0, pad_final: bool = True
) -> Iterator[Frame]:
    """Cut an array into pipeline frames.

    This is what lets a test drive the engine from a fixture: the frames it
    produces are indistinguishable from microphone frames.
    """
    if samples.dtype != np.float32:
        samples = samples.astype(np.float32)

    for offset in range(0, len(samples), FRAME_SAMPLES):
        chunk = samples[offset : offset + FRAME_SAMPLES]
        if len(chunk) < FRAME_SAMPLES:
            if not pad_final:
                return
            chunk = np.pad(chunk, (0, FRAME_SAMPLES - len(chunk)))
        yield Frame(samples=chunk, started_at=start_at + offset / SAMPLE_RATE)


def read_wav(path: Path | str) -> np.ndarray:
    """Read a mono 16 kHz WAV into float32.

    Deliberately strict about the format rather than resampling silently: a
    fixture at the wrong rate should fail loudly in a test, not quietly change
    what the test measures.
    """
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1:
            raise ValueError(f"{path}: expected mono, got {handle.getnchannels()} channels")
        if handle.getframerate() != SAMPLE_RATE:
            raise ValueError(f"{path}: expected {SAMPLE_RATE} Hz, got {handle.getframerate()}")
        if handle.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit samples")
        raw = handle.readframes(handle.getnframes())

    return (np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0).copy()


def write_wav(path: Path | str, samples: np.ndarray) -> None:
    """Write float32 audio as a mono 16 kHz 16-bit WAV."""
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
