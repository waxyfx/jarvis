"""Picovoice Porcupine, behind the same :class:`WakeWordProvider` contract.

Added as an alternative to :mod:`~atlas_voice.engines.openwakeword`, not a
replacement: both stay, and which one runs is a configuration choice. The
session logic cannot tell them apart.

Three differences from openWakeWord are load-bearing and none of them is
cosmetic.

**It needs an AccessKey.** Picovoice issues one from their console, and the
library refuses to start without it. That puts a vendor and a credential inside
the one component that listens continuously — the reason openWakeWord was
preferred to begin with. The key is read from the environment and never logged;
:class:`PorcupineActivationError` and friends are re-raised as
:class:`VoiceEngineError` with the type name only, so an activation failure
cannot print it.

**It does not support Russian.** The supported set is German, English, Spanish,
French, Italian, Japanese, Korean and Portuguese — checked against
``pvporcupine.VALID_LANGUAGES`` and confirmed in the published documentation.
"Jarvis" is a *built-in* English keyword, so bare "Jarvis" needs no console work
at all; «Джарвис» cannot be built, by the console or otherwise. A bilingual wake
word therefore cannot be Porcupine alone.

**It reports events, not scores.** openWakeWord emits a probability per window
and a threshold is applied afterwards, so one pass yields a whole curve.
Porcupine decides internally and returns only "fired" — its ``sensitivity`` is
set at construction. Sweeping it means building one detector per point, and the
two axes are not the same quantity even when both are called a threshold.
"""

from __future__ import annotations

import os
from typing import Any

import numpy as np

from atlas_voice.audio import FRAME_SAMPLES, SAMPLE_RATE
from atlas_voice.providers import Detection, VoiceEngineError

__all__ = ["ACCESS_KEY_ENV", "PorcupineWakeWord"]

#: Where the key is read from. It is never accepted as an argument in a place
#: that might be logged, and never written to a metrics file.
ACCESS_KEY_ENV = "ATLAS_PORCUPINE_ACCESS_KEY"


class PorcupineWakeWord:
    """Detects one built-in or custom keyword. Stateful; feed it every frame."""

    name = "porcupine"

    def __init__(
        self,
        *,
        keyword: str = "jarvis",
        keyword_path: str | None = None,
        sensitivity: float = 0.5,
        access_key: str | None = None,
        refractory_s: float = 1.0,
    ) -> None:
        """
        ``sensitivity`` trades misses against false alarms, 0 to 1, and unlike
        openWakeWord's threshold it is baked into the detector at construction.
        """
        if not 0.0 <= sensitivity <= 1.0:
            raise ValueError("sensitivity must be between 0 and 1")

        key = access_key or os.environ.get(ACCESS_KEY_ENV, "")
        if not key:
            raise VoiceEngineError(
                f"Porcupine needs an AccessKey. Put it in .env as {ACCESS_KEY_ENV}; "
                "it is issued free from https://console.picovoice.ai/"
            )

        try:
            import pvporcupine
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise VoiceEngineError("pvporcupine is required; install atlas-voice[wake]") from exc

        options: dict[str, Any] = {"sensitivities": [sensitivity]}
        if keyword_path:
            options["keyword_paths"] = [keyword_path]
        else:
            if keyword not in pvporcupine.KEYWORDS:
                raise VoiceEngineError(
                    f"{keyword!r} is not a built-in Porcupine keyword. "
                    f"Built in: {sorted(pvporcupine.KEYWORDS)}"
                )
            options["keywords"] = [keyword]

        try:
            self._engine = pvporcupine.create(access_key=key, **options)
        except Exception as exc:
            # Deliberately the type name only: the message from an activation
            # failure can quote the key back.
            raise VoiceEngineError(f"Porcupine refused to start: {type(exc).__name__}") from exc

        if self._engine.sample_rate != SAMPLE_RATE:
            raise VoiceEngineError(
                f"Porcupine wants {self._engine.sample_rate} Hz, the pipeline runs at {SAMPLE_RATE}"
            )

        self._frame = int(self._engine.frame_length)
        self._label = keyword
        self._refractory_s = refractory_s
        self._pending = np.zeros(0, dtype=np.float32)
        self._elapsed = 0.0
        self._quiet_until = 0.0
        #: Porcupine reports no score. 1.0 on a detection, 0.0 otherwise, so the
        #: field means the same thing structurally without pretending to a
        #: precision it does not have.
        self.last_score = 0.0

    def push(self, samples: np.ndarray) -> Detection | None:
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32)
        self._pending = np.concatenate([self._pending, samples])

        detection: Detection | None = None
        while len(self._pending) >= self._frame:
            chunk, self._pending = self._pending[: self._frame], self._pending[self._frame :]
            self._elapsed += self._frame / SAMPLE_RATE

            # Porcupine takes signed 16-bit PCM, and the pipeline carries
            # float32 in [-1, 1]. Clipping first: a sample above 1.0 would wrap
            # to a large negative on cast, which is white noise to the detector.
            pcm = (np.clip(chunk, -1.0, 1.0) * 32767.0).astype(np.int16)
            fired = self._engine.process(pcm) >= 0

            self.last_score = 1.0 if fired else 0.0
            if fired and self._elapsed >= self._quiet_until:
                self._quiet_until = self._elapsed + self._refractory_s
                detection = Detection(score=1.0, at=self._elapsed, label=self._label)
        return detection

    def reset(self) -> None:
        self._pending = np.zeros(0, dtype=np.float32)
        self._quiet_until = self._elapsed + self._refractory_s
        self.last_score = 0.0

    def scores_with_times(self, samples: np.ndarray) -> list[tuple[float, float]]:
        """``(stream_time, 1.0 or 0.0)`` per frame, ignoring the refractory gap.

        Shaped like openWakeWord's method so the acceptance harness can drive
        either. The values are events rather than probabilities, which the
        comparison has to say out loud rather than quietly average.
        """
        self.reset()
        self._elapsed = 0.0
        self._quiet_until = 0.0
        collected: list[tuple[float, float]] = []
        for offset in range(0, len(samples) - self._frame + 1, self._frame):
            self.push(samples[offset : offset + self._frame])
            collected.append((self._elapsed, self.last_score))
        return collected

    def scores_for(self, samples: np.ndarray) -> list[float]:
        return [score for _, score in self.scores_with_times(samples)]

    def close(self) -> None:
        """Release the native handle. Safe to call twice."""
        engine = getattr(self, "_engine", None)
        if engine is not None:
            self._engine = None
            engine.delete()

    def __del__(self) -> None:  # pragma: no cover - interpreter teardown
        # Nothing useful can be done about a failure here: the interpreter may
        # already be tearing down, and raising from __del__ only prints noise.
        # Swallowed deliberately rather than by omission.
        try:
            self.close()
        except Exception:  # noqa: S110
            pass


assert FRAME_SAMPLES == 512, "Porcupine's frame length is 512 at 16 kHz; keep the pipeline aligned"
