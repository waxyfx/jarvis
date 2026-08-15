"""Turning frame-by-frame speech decisions into whole utterances.

The VAD answers a narrow question — *is there speech in these 32 milliseconds* —
and that answer alone is not usable. People pause mid-sentence, say "мм", and
breathe; a segmenter that ended the turn at the first quiet frame would cut them
off constantly, and one that waited too long would feel unresponsive. The
difference between an assistant that is pleasant to talk to and one that is not
lives almost entirely in these two thresholds.

No model is imported here. The segmenter takes speech/not-speech booleans, so
its behaviour is tested exactly, with no audio and no ONNX runtime — which
matters, because "did it wait long enough" is the sort of property that silently
regresses.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from atlas_voice.audio import FRAME_MS, Frame
from atlas_voice.providers import SpeechChunk

__all__ = ["SegmenterConfig", "SpeechSegmenter"]


def _frames_for(ms: float) -> int:
    return max(1, round(ms / FRAME_MS))


@dataclass(frozen=True, slots=True)
class SegmenterConfig:
    """Timings, in milliseconds, because that is how they are reasoned about."""

    #: Speech must persist this long before an utterance opens. Rejects clicks,
    #: keyboard noise and door slams without costing anything: the pre-roll
    #: means the audio is not lost while we wait to be sure.
    start_after_ms: float = 96.0
    #: Silence this long ends the utterance. The single most felt number in the
    #: whole engine. Below ~500 ms it interrupts people who pause to think;
    #: above ~1 s it feels sluggish.
    end_after_ms: float = 700.0
    #: Audio kept from *before* speech was confirmed. Without it, the first
    #: consonant is clipped — "закрой" arrives as "акрой".
    preroll_ms: float = 300.0
    #: Utterances shorter than this are discarded as noise rather than sent.
    min_utterance_ms: float = 200.0
    #: A hard ceiling. A VAD stuck on "speech" — a fan, a stuck stream — must
    #: not accumulate audio forever, and a caller waiting on a turn deserves an
    #: answer even when the room never goes quiet.
    max_utterance_ms: float = 20_000.0

    def __post_init__(self) -> None:
        if self.min_utterance_ms >= self.max_utterance_ms:
            raise ValueError("min_utterance_ms must be below max_utterance_ms")


class SpeechSegmenter:
    """Feed it frames; it yields complete utterances.

    Stateful and order-dependent, like every other stage in the pipeline.
    """

    def __init__(self, config: SegmenterConfig | None = None) -> None:
        self._config = config or SegmenterConfig()
        self._start_frames = _frames_for(self._config.start_after_ms)
        self._end_frames = _frames_for(self._config.end_after_ms)
        self._preroll_frames = _frames_for(self._config.preroll_ms)
        self._max_frames = _frames_for(self._config.max_utterance_ms)

        self._recent: list[Frame] = []
        self._collected: list[Frame] = []
        self._speech_run = 0
        self._silence_run = 0
        self._open = False
        #: Set when the last utterance was closed by the ceiling rather than by
        #: silence. The caller may want to say so instead of pretending the
        #: person stopped talking.
        self.last_was_truncated = False

    @property
    def is_open(self) -> bool:
        """Whether an utterance is currently being collected."""
        return self._open

    def reset(self) -> None:
        self._recent.clear()
        self._collected.clear()
        self._speech_run = 0
        self._silence_run = 0
        self._open = False
        self.last_was_truncated = False

    def push(self, frame: Frame, *, is_speech: bool) -> SpeechChunk | None:
        """Feed one frame. Returns an utterance when one has just ended."""
        if is_speech:
            self._speech_run += 1
            self._silence_run = 0
        else:
            self._silence_run += 1
            self._speech_run = 0

        if not self._open:
            self._remember(frame)
            if self._speech_run >= self._start_frames:
                self._open_utterance()
            return None

        self._collected.append(frame)

        if len(self._collected) >= self._max_frames:
            self.last_was_truncated = True
            return self._close()

        if self._silence_run >= self._end_frames:
            self.last_was_truncated = False
            return self._close()

        return None

    def flush(self) -> SpeechChunk | None:
        """End any open utterance now — the stream closed, or mute was pressed.

        Whatever was being said is worth keeping: dropping it would lose a
        command the person had already finished speaking.
        """
        if not self._open:
            return None
        self.last_was_truncated = False
        return self._close()

    # -------------------------------------------------------------- internals

    def _remember(self, frame: Frame) -> None:
        self._recent.append(frame)
        if len(self._recent) > self._preroll_frames + self._start_frames:
            self._recent.pop(0)

    def _open_utterance(self) -> None:
        self._open = True
        # Everything buffered, including the frames that proved it was speech.
        self._collected = list(self._recent)
        self._recent.clear()

    def _close(self) -> SpeechChunk | None:
        frames = self._collected
        self._collected = []
        self._recent.clear()
        self._open = False
        silence_run, self._silence_run = self._silence_run, 0
        self._speech_run = 0

        if not frames:
            return None

        # Drop the trailing silence that ended the turn, but keep a little so
        # the final consonant is not clipped.
        keep_tail = _frames_for(100.0)
        if silence_run > keep_tail:
            trimmed = frames[: -(silence_run - keep_tail)]
            frames = trimmed or frames

        duration_ms = len(frames) * FRAME_MS
        if duration_ms < self._config.min_utterance_ms:
            return None

        return SpeechChunk(
            samples=np.concatenate([frame.samples for frame in frames]),
            started_at=frames[0].started_at,
            ended_at=frames[-1].ends_at,
        )
